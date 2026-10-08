from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx

from .db import utcnow

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
        self.base = f"https://api.telegram.org/bot{self.token}"

    def call(self, method: str, **data):
        try:
            response = httpx.post(f"{self.base}/{method}", json=data, timeout=20)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Telegram {method} HTTP failure ({type(exc).__name__})") from None
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {result.get('description')}")
        return result.get("result")
    def send(self, chat_id: str, message: str):
        # Plain text prevents source content from injecting HTML/Markdown markup.
        return self.call("sendMessage", chat_id=chat_id, text=message[:4096], disable_web_page_preview=True)

    def updates(self, offset: int, timeout: int = 25):
        try:
            response = httpx.get(
                f"{self.base}/getUpdates",
                params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
                timeout=timeout + 10,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Telegram getUpdates HTTP failure ({type(exc).__name__})") from None
        return response.json().get("result", [])

def send_once(conn, telegram: Telegram, chat_id: str, text: str, key: str, *, news_id: int | None = None, kind: str = "digest") -> bool:
    with conn:
        existing = conn.execute("SELECT status FROM notifications WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO notifications(news_id,kind,chat_id,dedupe_key,status,created_at) VALUES(?,?,?,?,?,?)",
            (news_id, kind, chat_id, key, "attempting", utcnow()),
        )
    try:
        telegram.send(chat_id, text)
    except Exception as exc:
        # A transport timeout is ambiguous: Telegram may have accepted the message.
        # No automatic resend of attempted messages, preserving at-most-once behavior.
        with conn:
            conn.execute("UPDATE notifications SET status='uncertain',error=? WHERE dedupe_key=?", (str(exc)[:500], key))
        raise
    with conn:
        conn.execute("UPDATE notifications SET status='sent',sent_at=? WHERE dedupe_key=?", (utcnow(), key))
        if news_id:
            conn.execute("UPDATE news SET notification_status='sent' WHERE id=?", (news_id,))
    return True


def format_news(row) -> str:
    isins = row["isins"] or "нет привязанных ISIN"
    summary = (row["body"] or "")[:500].strip()
    if not summary:
        summary = "Краткое содержание недоступно."
    return (
        f"{row['severity']} · {row['issuer_name']}\n"
        f"ISIN: {isins}\n"
        f"{row['title']}\n"
        f"Опубликовано: {row['published_at'] or 'не указано'}\n"
        f"{summary}\n"
        f"Источник: {row['url']}"
    )


def digest_parts(conn, *, since: str | None = None, until: str | None = None) -> list[str]:
    if since is None:
        since = datetime.fromtimestamp(0, timezone.utc).isoformat()
    if until is None:
        until = utcnow()
    rows = conn.execute(
        """SELECT n.*, GROUP_CONCAT(DISTINCT i.name) issuer_name,
                  GROUP_CONCAT(DISTINCT b.isin) isins
           FROM news n JOIN news_issuers ni ON ni.news_id=n.id
           JOIN issuers i ON i.id=ni.issuer_id
           LEFT JOIN news_bonds nb ON nb.news_id=n.id
           LEFT JOIN bonds b ON b.isin=nb.isin
           WHERE n.discovered_at>? AND n.discovered_at<=?
             AND n.id=(SELECT MIN(x.id) FROM news x WHERE x.fingerprint=n.fingerprint)
           GROUP BY n.id ORDER BY CASE n.severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
           n.discovered_at DESC""",
        (since, until),
    ).fetchall()
    failures = conn.execute("SELECT name,last_error FROM sources WHERE enabled=1 AND status!='ok'").fetchall()
    checked = conn.execute("SELECT name FROM sources WHERE enabled=1 AND status='ok'").fetchall()
    header = f"Сводка Bond Watch: {len(rows)} событий" if rows else "Новых событий нет."
    if not rows:
        header += "\nУспешно проверены: " + (", ".join(r["name"] for r in checked) or "нет")
    if failures:
        header += "\nНе удалось проверить: " + "; ".join(
            f"{r['name']} ({r['last_error'] or 'нет успешного опроса'})" for r in failures
        )
    parts = [header[:3900]]
    for row in rows:
        block = format_news(row)[:3800]
        if len(parts[-1]) + len(block) + 2 > 3900:
            parts.append(block)
        else:
            parts[-1] += "\n\n" + block
    return parts


def digest_text(conn, *, since: str | None = None, until: str | None = None) -> str:
    return "\n\n".join(digest_parts(conn, since=since, until=until))

def run_bot(conn, root, settings, poll_callback):
    from .config import ISIN_RE, load_portfolio, save_portfolio
    bot = Telegram()
    allowed = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
    if not allowed:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS is empty")
    offset = int((conn.execute("SELECT value FROM bot_state WHERE key='telegram_offset'").fetchone() or ["0"])[0])
    while True:
        try:
            for update in bot.updates(offset):
                offset = max(offset, update["update_id"] + 1)
                # Persist before acting to prevent a command replay on restart.
                with conn:
                    conn.execute("INSERT OR REPLACE INTO bot_state(key,value) VALUES('telegram_offset',?)", (str(offset),))
                message = update.get("message") or {}
                user_id = (message.get("from") or {}).get("id")
                if user_id not in allowed:
                    continue
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = (message.get("text") or "").strip()
                command, _, arg = text.partition(" ")
                command = command.split("@")[0].lower()
                if command == "/start":
                    reply = "Bond Watch: /status /portfolio /sources /digest /add ISIN /remove ISIN"
                elif command == "/status":
                    runs = conn.execute("SELECT source_id,status,finished_at,error FROM collector_runs ORDER BY id DESC LIMIT 8").fetchall()
                    reply = "\n".join(f"{r['source_id']}: {r['status']} {r['finished_at'] or ''} {r['error'] or ''}" for r in runs) or "Опросов пока нет."
                elif command == "/sources":
                    rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
                    reply = "\n".join(f"{r['name']}: {r['status']} {r['last_success_at'] or ''} {r['last_error'] or ''}" for r in rows)
                elif command == "/portfolio":
                    rows = conn.execute("SELECT b.isin,b.status,b.rating,b.rating_agency,b.rating_date,i.name FROM bonds b LEFT JOIN issuers i ON i.id=b.issuer_id ORDER BY b.isin").fetchall()
                    reply = "\n".join(f"{r['isin']} — {r['name'] or 'unresolved'} ({r['status']}); {r['rating_agency'] or ''} {r['rating'] or ''} {r['rating_date'] or ''}" for r in rows)
                elif command == "/digest":
                    for part in digest_parts(conn):
                        bot.send(chat_id, part)
                    continue
                elif command in {"/add", "/remove"}:
                    isin = arg.strip().upper()
                    if not ISIN_RE.fullmatch(isin):
                        reply = "Укажите ISIN в формате RUXXXXXXXXXX."
                    else:
                        values = load_portfolio(root)
                        if command == "/add" and isin not in values:
                            values.append(isin)
                            save_portfolio(root, values)
                            with conn:
                                conn.execute("INSERT OR IGNORE INTO bonds(isin) VALUES(?)", (isin,))
                            reply = f"{isin} добавлен; проверка через ISS при следующем resolve."
                        elif command == "/remove" and isin in values:
                            values.remove(isin)
                            save_portfolio(root, values)
                            with conn:
                                conn.execute("DELETE FROM price_observations WHERE isin=?", (isin,))
                                conn.execute("DELETE FROM bonds WHERE isin=?", (isin,))
                            reply = f"{isin} удален."
                        else:
                            reply = "Портфель не изменился."
                else:
                    reply = "Неизвестная команда. /start — справка."
                bot.send(chat_id, reply)
        except Exception:
            log.exception("Telegram bot loop failed")
            time.sleep(5)









