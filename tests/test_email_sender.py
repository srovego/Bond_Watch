from __future__ import annotations

import smtplib
import ssl

import pytest

from bond_watch.cli import main
from bond_watch.db import connect
from bond_watch.email_sender import (DIGEST_SUBJECT, TEST_SUBJECT, EmailConfig,
                                     EmailDeliveryError, send_digest_once, send_email)


class FakeSMTP:
    instances = []
    failure = None

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.messages = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, *, context):
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append("login")
        if self.failure == "login":
            raise smtplib.SMTPAuthenticationError(535, b"credentials rejected")
        assert username == "user@gmail.com" and password == "dummy-secret"

    def send_message(self, message):
        self.calls.append("send")
        if self.failure == "send":
            raise TimeoutError("ambiguous after DATA")
        if self.failure == "partial":
            return {"reader@example.com": (550, b"rejected")}
        self.messages.append(message)
        return {}


@pytest.fixture
def smtp_env(monkeypatch):
    values = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
              "SMTP_USERNAME": "user@gmail.com", "SMTP_PASSWORD": "dummy-secret",
              "SMTP_FROM": "Bond Watch <user@gmail.com>", "SMTP_TO": "reader@example.com",
              "SMTP_USE_STARTTLS": "true"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    FakeSMTP.instances = []
    FakeSMTP.failure = None
    monkeypatch.setattr("bond_watch.email_sender.smtplib.SMTP", FakeSMTP)
    return values


def test_starttls_utf8_russian_links_and_no_password_in_message(smtp_env):
    config = EmailConfig.from_env()
    send_email(config, DIGEST_SUBJECT, "Отчёт РЖД: https://www.moex.com/n104954")
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.gmail.com", 587, 15)
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login", "send"]
    message = smtp.messages[0]
    assert message["Subject"] == DIGEST_SUBJECT
    assert message.get_content_charset() == "utf-8"
    assert "Отчёт РЖД" in message.get_content()
    assert "https://www.moex.com/n104954" in message.get_content()
    assert "dummy-secret" not in message.as_string()


def test_insecure_settings_are_rejected_before_connection(smtp_env, monkeypatch):
    monkeypatch.setenv("SMTP_USE_STARTTLS", "false")
    with pytest.raises(ValueError, match="STARTTLS"):
        EmailConfig.from_env()
    assert FakeSMTP.instances == []


def test_uncertain_delivery_is_not_retried(tmp_path, smtp_env):
    conn = connect(tmp_path / "mail.db")
    FakeSMTP.failure = "send"
    with pytest.raises(EmailDeliveryError, match="uncertain") as error:
        send_digest_once(conn, EmailConfig.from_env(), "Одна сводка")
    assert "dummy-secret" not in str(error.value)
    FakeSMTP.failure = None
    assert send_digest_once(conn, EmailConfig.from_env(), "Одна сводка") is False
    assert len(FakeSMTP.instances) == 1
    row = conn.execute("SELECT kind,status,error FROM notifications").fetchone()
    assert row["kind"] == "email_digest" and row["status"] == "uncertain"
    assert "dummy-secret" not in row["error"]
    conn.close()


def test_definite_login_failure_can_retry_and_success_dedupes(tmp_path, smtp_env):
    conn = connect(tmp_path / "mail.db")
    FakeSMTP.failure = "login"
    with pytest.raises(EmailDeliveryError, match="not started"):
        send_digest_once(conn, EmailConfig.from_env(), "Сводка")
    assert conn.execute("SELECT status FROM notifications").fetchone()[0] == "failed"
    FakeSMTP.failure = None
    assert send_digest_once(conn, EmailConfig.from_env(), "Сводка") is True
    assert send_digest_once(conn, EmailConfig.from_env(), "Сводка") is False
    assert len(FakeSMTP.instances) == 2
    assert conn.execute("SELECT status FROM notifications").fetchone()[0] == "sent"
    conn.close()


def test_cli_check_email_and_digest_email_without_real_smtp(tmp_path, smtp_env, capsys, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text(
        "database: data/bond_watch.db\nlog_file: data/bond_watch.log\nsources: {}\n",
        encoding="utf-8")
    (config_dir / "portfolio.yaml").write_text("isins: []\n", encoding="utf-8")
    (config_dir / "verified_bonds.yaml").write_text("bonds: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(f"{name}={value}" for name, value in smtp_env.items()) + "\n",
        encoding="utf-8")
    for name in smtp_env:
        monkeypatch.delenv(name)
    assert main(["--root", str(tmp_path), "check-email"]) == 0
    assert FakeSMTP.instances[0].messages[0]["Subject"] == TEST_SUBJECT
    assert main(["--root", str(tmp_path), "digest", "--email"]) == 0
    assert main(["--root", str(tmp_path), "digest", "--email"]) == 0
    assert len(FakeSMTP.instances) == 2
    assert "Email digest already handled" in capsys.readouterr().out
    conn = connect(tmp_path / "data/bond_watch.db")
    assert conn.execute("SELECT count(*) FROM notifications WHERE kind='email_digest'").fetchone()[0] == 1
    conn.close()


def test_interrupted_attempt_is_not_replayed(tmp_path, smtp_env, monkeypatch):
    conn = connect(tmp_path / "mail.db")
    def interrupted(*_):
        raise SystemExit("process stopped after journal claim")
    monkeypatch.setattr("bond_watch.email_sender.send_email", interrupted)
    with pytest.raises(SystemExit):
        send_digest_once(conn, EmailConfig.from_env(), "Важная сводка")
    assert conn.execute("SELECT status FROM notifications").fetchone()[0] == "attempting"
    assert send_digest_once(conn, EmailConfig.from_env(), "Важная сводка") is False
    conn.close()


def test_partial_recipient_failure_is_uncertain(tmp_path, smtp_env):
    conn = connect(tmp_path / "mail.db")
    FakeSMTP.failure = "partial"
    with pytest.raises(EmailDeliveryError, match="uncertain"):
        send_digest_once(conn, EmailConfig.from_env(), "Письмо")
    assert conn.execute("SELECT status FROM notifications").fetchone()[0] == "uncertain"
    conn.close()
