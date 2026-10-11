"""Gmail SMTP delivery with a durable at-most-once digest journal."""
from __future__ import annotations

import hashlib
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses

from .db import utcnow

SMTP_TIMEOUT_SECONDS = 15
TEST_SUBJECT = "Bond Watch — SMTP test"
DIGEST_SUBJECT = "Bond Watch — сводка"


class EmailDeliveryError(RuntimeError):
    def __init__(self, stage: str, *, uncertain: bool = False):
        self.uncertain = uncertain
        super().__init__(f"SMTP {stage} failed; delivery {'uncertain' if uncertain else 'not started'}")


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "EmailConfig":
        names = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                 "SMTP_FROM", "SMTP_TO", "SMTP_USE_STARTTLS")
        missing = [name for name in names if not os.getenv(name)]
        if missing:
            raise ValueError("Missing SMTP settings: " + ", ".join(missing))
        host = os.environ["SMTP_HOST"].strip().lower()
        port = os.environ["SMTP_PORT"].strip()
        starttls = os.environ["SMTP_USE_STARTTLS"].strip().lower()
        if host != "smtp.gmail.com" or port != "587" or starttls not in {"true", "1", "yes"}:
            raise ValueError("Gmail SMTP requires smtp.gmail.com:587 and SMTP_USE_STARTTLS=true")
        sender = os.environ["SMTP_FROM"].strip()
        recipient_setting = os.environ["SMTP_TO"]
        recipients = tuple(address for _, address in getaddresses([recipient_setting]) if address)
        from_addresses = getaddresses([sender])
        if (len(from_addresses) != 1 or "@" not in from_addresses[0][1]
                or not recipients or any("@" not in address for address in recipients)
                or "\n" in sender or "\r" in sender
                or "\n" in recipient_setting or "\r" in recipient_setting):
            raise ValueError("SMTP_FROM and SMTP_TO must contain valid email addresses")
        return cls(host, 587, os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"], sender, recipients)


def send_email(config: EmailConfig, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Subject"] = subject
    message.set_content(body, charset="utf-8")
    stage = "connection"
    try:
        with smtplib.SMTP(config.host, config.port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.ehlo()
            stage = "STARTTLS"
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            stage = "authentication"
            smtp.login(config.username, config.password)
            stage = "delivery"
            refused = smtp.send_message(message)
            if refused:
                raise EmailDeliveryError("partial delivery", uncertain=True)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        # Never expose SMTP credentials or server responses in logs or SQLite.
        raise EmailDeliveryError(stage, uncertain=stage == "delivery") from None


def send_digest_once(conn, config: EmailConfig, body: str) -> bool:
    """Claim a content key before sending; uncertain outcomes are never retried."""
    digest = hashlib.sha256((DIGEST_SUBJECT + "\n" + ",".join(config.recipients) + "\n" + body).encode("utf-8")).hexdigest()
    key = f"email:digest:{digest}"
    with conn:
        existing = conn.execute("SELECT status FROM notifications WHERE dedupe_key=?", (key,)).fetchone()
        if existing and existing["status"] != "failed":
            return False
        if existing:
            conn.execute("UPDATE notifications SET status='attempting',error=NULL WHERE dedupe_key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO notifications(kind,chat_id,dedupe_key,status,created_at) VALUES(?,?,?,?,?)",
                ("email_digest", ",".join(config.recipients), key, "attempting", utcnow()),
            )
    try:
        send_email(config, DIGEST_SUBJECT, body)
    except EmailDeliveryError as exc:
        with conn:
            conn.execute("UPDATE notifications SET status=?,error=? WHERE dedupe_key=?",
                         ("uncertain" if exc.uncertain else "failed", str(exc), key))
        raise
    with conn:
        conn.execute("UPDATE notifications SET status='sent',sent_at=? WHERE dedupe_key=?", (utcnow(), key))
    return True
