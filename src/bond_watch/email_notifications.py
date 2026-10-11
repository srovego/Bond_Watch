"""Automatic email queue for credit alerts and daily source reports."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import utcnow
from .email_sender import EmailConfig, EmailDeliveryError, send_email

log = logging.getLogger(__name__)
MAX_AUTO_ATTEMPTS = 3
RETRY_BASE_MINUTES = 15
MOSCOW = ZoneInfo("Europe/Moscow")
ROUTINE_CATEGORIES = ("trading_restriction", "technical_market_notice")


def _event_key(row) -> str:
    # Existing fingerprints include normalized title, publication day and issuer.
    # Group exact re-publications without hiding distinct actions on one day.
    return f"fingerprint:{row['fingerprint']}"


def _enqueue(conn, *, kind: str, event_key: str, recipient: str, subject: str,
             body: str, news_id: int | None = None) -> bool:
    dedupe = hashlib.sha256(f"{kind}\0{event_key}\0{recipient.lower()}".encode()).hexdigest()
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO notifications(news_id,kind,chat_id,dedupe_key,status,created_at,
            event_key,recipient,subject,body,result)
            VALUES(?,?,?,?,'pending',?,?,?,?,?,'queued')""",
            (news_id, kind, recipient, f"email:auto:{dedupe}", utcnow(), event_key,
             recipient, subject, body),
        )
    return bool(cur.rowcount)


def _news_rows(conn, where: str, params: tuple):
    return conn.execute(
        """SELECT n.*,ni.issuer_id,i.name issuer_name,GROUP_CONCAT(DISTINCT b.isin) isins
        FROM news n JOIN news_issuers ni ON ni.news_id=n.id
        JOIN issuers i ON i.id=ni.issuer_id
        LEFT JOIN news_bonds nb ON nb.news_id=n.id
        LEFT JOIN bonds b ON b.isin=nb.isin
        WHERE """ + where + """ AND n.category NOT IN (?,?)
        AND n.category!='price_move'
        AND n.id=(SELECT MIN(x.id) FROM news x WHERE x.fingerprint=n.fingerprint)
        GROUP BY n.id,ni.issuer_id ORDER BY n.id""",
        (*params, *ROUTINE_CATEGORIES),
    ).fetchall()


def _event_body(row) -> str:
    text = (row["body"] or "").strip()[:800]
    lines = [f"{row['severity']} · {row['issuer_name']}", row["title"],
             f"Опубликовано: {row['published_at'] or 'не указано'}"]
    if row["event_date"]:
        lines.append(f"Дата события: {row['event_date']}")
    if row["mentioned_isin"] or row["isins"]:
        lines.append(f"ISIN события: {row['mentioned_isin'] or '-'}; портфель: {row['isins'] or '-'}")
    if text:
        lines.append(text)
    if row["classification_basis"]:
        lines.append(f"Основание классификации: {row['classification_basis']}")
    lines.append(f"Источник: {row['url']}")
    return "\n".join(lines)


def enqueue_poll_alerts(conn, after_news_id: int, recipients: tuple[str, ...]) -> int:
    rows = _news_rows(conn, "n.id>? AND n.severity IN ('CRITICAL','WARNING') AND n.category!='price_move'",
                      (after_news_id,))
    queued = 0
    for row in rows:
        key = _event_key(row)
        subject = f"Bond Watch — {row['severity']}: {row['issuer_name']}"[:180]
        for recipient in recipients:
            queued += _enqueue(conn, kind="email_urgent", event_key=key, recipient=recipient,
                               subject=subject, body=_event_body(row), news_id=row["id"])
    return queued


def daily_slot(now: datetime) -> tuple[str, datetime, datetime] | None:
    """Accept only the current Moscow slot; never backfill missed timer runs."""
    if now.tzinfo is None:
        raise ValueError("daily digest time must include a timezone")
    local = now.astimezone(MOSCOW)
    if 12 <= local.hour < 14:
        end = local.replace(hour=12, minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=17)
        name = "12"
    elif 19 <= local.hour < 21:
        end = local.replace(hour=19, minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=7)
        name = "19"
    else:
        return None
    return name, start, end


def enqueue_daily_digest(conn, recipients: tuple[str, ...], *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    slot = daily_slot(now)
    if slot is None:
        return 0
    name, start, end = slot
    rows = _news_rows(conn, "julianday(COALESCE(n.published_at,n.discovered_at))>=julianday(?) AND julianday(COALESCE(n.published_at,n.discovered_at))<julianday(?)",
                      (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                       end.astimezone(timezone.utc).isoformat(timespec="seconds")))
    unique = {}
    for row in rows:
        unique.setdefault(_event_key(row), row)
    ordered = sorted(unique.values(), key=lambda r: ({"CRITICAL": 0, "WARNING": 1}.get(r["severity"], 2), -r["id"]))
    lines = [f"Bond Watch — события с {start:%d.%m.%Y %H:%M} до {end:%d.%m.%Y %H:%M} МСК",
             f"Событий: {len(ordered)}"]
    for row in ordered:
        lines.extend(("", _event_body(row)))
    lines.extend(("", "Состояние источников:"))
    for source in conn.execute("SELECT name,status,last_success_at,last_error FROM sources WHERE enabled=1 ORDER BY id"):
        status = f"{source['name']}: {source['status']}"
        if source["last_success_at"]:
            status += f"; последняя проверка {source['last_success_at']}"
        if source["last_error"]:
            status += f"; ошибка {source['last_error'][:180]}"
        lines.append(status)
    if lines[-1] == "Состояние источников:":
        lines.append("Нет включенных источников")
    body = "\n".join(lines)
    event_key = f"daily:{end.date().isoformat()}:{name}"
    return sum(_enqueue(conn, kind="email_daily", event_key=event_key, recipient=recipient,
                        subject=f"Bond Watch — сводка {end:%d.%m.%Y} {name}:00 МСК", body=body)
               for recipient in recipients)


def deliver_due(conn, config: EmailConfig, *, now: datetime | None = None, limit: int = 50,
                allow_stale_daily: bool = False, only_id: int | None = None) -> dict[str, int]:
    """Claim each message durably before SMTP. Interrupted attempts stay unresolved."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    counts = {"sent": 0, "retryable": 0, "failed": 0, "uncertain": 0}
    if not allow_stale_daily:
        stale = conn.execute(
            """SELECT id,event_key FROM notifications WHERE kind='email_daily'
            AND status IN ('pending','retryable')"""
        ).fetchall()
        for row in stale:
            try:
                _, day, slot_hour = row["event_key"].split(":")
                end = datetime.fromisoformat(day).replace(tzinfo=MOSCOW, hour=int(slot_hour))
                expired = now > (end + timedelta(hours=2)).astimezone(timezone.utc)
            except (AttributeError, ValueError):
                expired = True
            if expired:
                with conn:
                    conn.execute(
                        """UPDATE notifications SET status='failed',result='stale_slot',
                        error='daily slot expired; manual safe retry available'
                        WHERE id=? AND status IN ('pending','retryable')""", (row["id"],))
    rows = conn.execute(
        """SELECT id FROM notifications WHERE kind IN ('email_urgent','email_daily')
        AND status IN ('pending','retryable')
        AND (next_attempt_at IS NULL OR next_attempt_at<=?)
        AND (? IS NULL OR id=?)
        ORDER BY CASE kind WHEN 'email_urgent' THEN 0 ELSE 1 END, id LIMIT ?""",
        (stamp, only_id, only_id, limit),
    ).fetchall()
    for candidate in rows:
        # Commit the claim before SMTP; interrupted attempts remain unresolved.
        with conn:
            cur = conn.execute(
                """UPDATE notifications SET status='attempting',attempt_count=attempt_count+1,
                last_attempt_at=?,next_attempt_at=NULL,result='smtp_attempt_started',error=NULL
                WHERE id=? AND status IN ('pending','retryable')""",
                (stamp, candidate["id"]),
            )
        if not cur.rowcount:
            continue
        row = conn.execute("SELECT * FROM notifications WHERE id=?", (candidate["id"],)).fetchone()
        try:
            send_email(replace(config, recipients=(row["recipient"],)), row["subject"], row["body"])
        except EmailDeliveryError as exc:
            if exc.uncertain:
                status, next_attempt = "uncertain", None
            elif exc.retryable and row["attempt_count"] < MAX_AUTO_ATTEMPTS:
                status = "retryable"
                next_attempt = (now + timedelta(minutes=RETRY_BASE_MINUTES * row["attempt_count"])).isoformat(timespec="seconds")
            else:
                status, next_attempt = "failed", None
            with conn:
                conn.execute(
                    "UPDATE notifications SET status=?,next_attempt_at=?,result=?,error=? WHERE id=?",
                    (status, next_attempt, str(exc), str(exc), row["id"]),
                )
            counts[status] += 1
            log.warning("Email notification %s: %s", row["id"], status)
        except Exception:
            # Unknown failures during/after SMTP are never replayed automatically.
            with conn:
                conn.execute("UPDATE notifications SET status='uncertain',result='unexpected_delivery_error',error='unexpected_delivery_error' WHERE id=?", (row["id"],))
            counts["uncertain"] += 1
            log.error("Email notification %s has unknown SMTP outcome", row["id"])
        else:
            with conn:
                conn.execute("UPDATE notifications SET status='sent',sent_at=?,result='sent',error=NULL WHERE id=?", (utcnow(), row["id"]))
            counts["sent"] += 1
    return counts


def process_poll_email(conn, after_news_id: int) -> dict[str, int]:
    """Email failure cannot abort a completed news/price poll."""
    try:
        config = EmailConfig.from_env()
        queued = enqueue_poll_alerts(conn, after_news_id, config.recipients)
        result = deliver_due(conn, config)
        log.info("Email poll notifications: queued=%s outcomes=%s", queued, result)
        return {"queued": queued, **result}
    except Exception as exc:
        log.error("Email poll notification processing failed: %s", type(exc).__name__)
        return {"error": 1}


def process_daily_email(conn) -> dict[str, int]:
    config = EmailConfig.from_env()
    queued = enqueue_daily_digest(conn, config.recipients)
    return {"queued": queued, **deliver_due(conn, config)}


def retry_safe(conn, config: EmailConfig, notification_id: int | None = None) -> dict[str, int]:
    """Replay only failures known to have happened before SMTP delivery."""
    condition = " AND id=?" if notification_id is not None else ""
    params = (notification_id,) if notification_id is not None else ()
    with conn:
        cur = conn.execute(
            """UPDATE notifications SET status='pending',attempt_count=0,
            next_attempt_at=NULL,result='manual_retry_queued',error=NULL
            WHERE kind IN ('email_urgent','email_daily') AND status IN ('failed','retryable')""" + condition,
            params,
        )
    return {"requeued": cur.rowcount, **deliver_due(conn, config, allow_stale_daily=True, only_id=notification_id)}
