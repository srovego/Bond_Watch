from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bond_watch.db import connect
from bond_watch.email_notifications import (deliver_due, enqueue_daily_digest,
    enqueue_poll_alerts, process_poll_email, retry_safe)
from bond_watch.email_sender import EmailConfig, EmailDeliveryError


def setup_db(tmp_path):
    conn = connect(tmp_path / 'alerts.sqlite')
    with conn:
        conn.execute("INSERT INTO sources(id,name,enabled,status,last_success_at) VALUES('moex_news','MOEX',1,'ok','2026-10-12T15:00:00+00:00')")
        conn.execute("INSERT INTO sources(id,name,enabled,status,last_error) VALUES('cbr_rss','CBR',1,'error','timeout')")
        issuer = conn.execute("INSERT INTO issuers(name,inn) VALUES('ОАО РЖД','7708503727')").lastrowid
        conn.execute("INSERT INTO bonds(isin,issuer_id,status) VALUES('RU000A10AZ60',?,'verified')", (issuer,))
    return conn, issuer


def news(conn, issuer, item, *, severity='WARNING', category='rating', title='РЖД: снижение рейтинга', published='2026-10-12T12:00:00+00:00', fingerprint=None):
    with conn:
        news_id = conn.execute(
            """INSERT INTO news(source_id,source_item_id,url,source_name,published_at,discovered_at,title,body,category,severity,fingerprint,classification_basis)
            VALUES('moex_news',?,?,?,?,?,?,?,?,?,?,?)""",
            (item, f'https://www.moex.com/{item}', 'MOEX', published, published, title,
             'Подтверждено рейтинговое действие', category, severity, fingerprint or item, 'rating_downgrade'),
        ).lastrowid
        conn.execute('INSERT INTO news_issuers(news_id,issuer_id) VALUES(?,?)', (news_id, issuer))
        conn.execute("INSERT INTO news_bonds(news_id,isin) VALUES(?,'RU000A10AZ60')", (news_id,))
    return news_id


def config(recipients=('a@example.com',)):
    return EmailConfig('smtp.gmail.com', 587, 'user', 'secret', 'user@gmail.com', recipients)


def test_urgent_only_new_credit_events_and_one_per_recipient(tmp_path, monkeypatch):
    conn, issuer = setup_db(tmp_path)
    news(conn, issuer, 'price', category='price_move')
    news(conn, issuer, 'info', severity='INFO', category='financial_results')
    news(conn, issuer, 'credit')
    news(conn, issuer, 'duplicate', fingerprint='credit')
    assert enqueue_poll_alerts(conn, 0, ('a@example.com', 'b@example.com')) == 2
    assert enqueue_poll_alerts(conn, 0, ('a@example.com', 'b@example.com')) == 0
    sent = []
    monkeypatch.setattr('bond_watch.email_notifications.send_email', lambda cfg, subject, body: sent.append((cfg.recipients, subject, body)))
    assert deliver_due(conn, config())['sent'] == 2
    assert len(sent) == 2 and all(len(item[0]) == 1 for item in sent)
    assert 'https://www.moex.com/credit' in sent[0][2]
    assert 'RU000A10AZ60' in sent[0][2]
    assert conn.execute("SELECT count(*) FROM notifications WHERE status='sent'").fetchone()[0] == 2
    conn.close()


def test_unknown_smtp_result_is_never_retried(tmp_path, monkeypatch):
    conn, issuer = setup_db(tmp_path)
    news(conn, issuer, 'credit')
    enqueue_poll_alerts(conn, 0, config().recipients)
    calls = []
    def uncertain(*args):
        calls.append(1)
        raise EmailDeliveryError('delivery', uncertain=True)
    monkeypatch.setattr('bond_watch.email_notifications.send_email', uncertain)
    assert deliver_due(conn, config())['uncertain'] == 1
    assert deliver_due(conn, config())['uncertain'] == 0
    assert retry_safe(conn, config(), 1)['requeued'] == 0
    assert len(calls) == 1
    row = conn.execute('SELECT status,attempt_count,last_attempt_at,result FROM notifications').fetchone()
    assert row['status'] == 'uncertain' and row['attempt_count'] == 1 and row['last_attempt_at']
    conn.close()


def test_definite_transient_failure_has_bounded_retries(tmp_path, monkeypatch):
    conn, issuer = setup_db(tmp_path)
    news(conn, issuer, 'credit')
    enqueue_poll_alerts(conn, 0, config().recipients)
    monkeypatch.setattr('bond_watch.email_notifications.send_email', lambda *args: (_ for _ in ()).throw(EmailDeliveryError('connection', retryable=True)))
    now = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)
    assert deliver_due(conn, config(), now=now)['retryable'] == 1
    assert deliver_due(conn, config(), now=now)['retryable'] == 0
    assert deliver_due(conn, config(), now=now + timedelta(minutes=15))['retryable'] == 1
    assert deliver_due(conn, config(), now=now + timedelta(minutes=45))['failed'] == 1
    row = conn.execute('SELECT status,attempt_count,next_attempt_at FROM notifications').fetchone()
    assert row['status'] == 'failed' and row['attempt_count'] == 3 and row['next_attempt_at'] is None
    monkeypatch.setattr('bond_watch.email_notifications.send_email', lambda *args: None)
    assert retry_safe(conn, config(), 1)['sent'] == 1
    conn.close()


def test_daily_24h_sources_and_deduped_moscow_day(tmp_path, monkeypatch):
    conn, issuer = setup_db(tmp_path)
    news(conn, issuer, 'old', published='2026-10-11T10:00:00+00:00')
    news(conn, issuer, 'fresh', severity='INFO', category='financial_results')
    now = datetime(2026, 10, 12, 16, tzinfo=timezone.utc)
    assert enqueue_daily_digest(conn, config().recipients, now=now) == 1
    assert enqueue_daily_digest(conn, config().recipients, now=now) == 0
    row = conn.execute("SELECT body,event_key FROM notifications WHERE kind='email_daily'").fetchone()
    assert 'https://www.moex.com/fresh' in row['body']
    assert 'https://www.moex.com/old' not in row['body']
    assert 'CBR: error' in row['body'] and 'MOEX: ok' in row['body']
    assert row['event_key'] == 'daily:2026-10-12'
    conn.close()


def test_poll_email_failure_does_not_abort_collection(tmp_path, monkeypatch):
    conn, issuer = setup_db(tmp_path)
    news(conn, issuer, 'credit')
    monkeypatch.delenv('SMTP_PASSWORD', raising=False)
    assert process_poll_email(conn, 0) == {'error': 1}
    assert conn.execute('SELECT count(*) FROM news').fetchone()[0] == 1
    conn.close()


def test_migration_preserves_old_notifications(tmp_path):
    import sqlite3
    from bond_watch.db import MIGRATIONS
    path = tmp_path / 'legacy.sqlite'
    raw = sqlite3.connect(path)
    for version, migration in enumerate(MIGRATIONS[:7], 1):
        raw.executescript(migration + f'\nPRAGMA user_version={version};')
    raw.execute("INSERT INTO notifications(kind,chat_id,dedupe_key,status,created_at) VALUES('email_digest','a@example.com','old','sent','2026-10-01')")
    raw.commit(); raw.close()
    conn = connect(path)
    row = conn.execute("SELECT status,event_key,attempt_count FROM notifications WHERE dedupe_key='old'").fetchone()
    assert row['status'] == 'sent' and row['event_key'] is None and row['attempt_count'] == 0
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 8
    conn.close()


def test_poll_calls_email_after_collectors_without_changing_news(tmp_path, monkeypatch):
    from bond_watch import pipeline
    conn, issuer = setup_db(tmp_path)
    observed = []
    class FakeHttp:
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(pipeline, 'Http', FakeHttp)
    monkeypatch.setattr(pipeline, 'sync_portfolio', lambda *args: None)
    monkeypatch.setattr(pipeline, 'seed_verified_bonds', lambda *args: None)
    monkeypatch.setattr(pipeline, 'sync_related_companies', lambda *args: None)
    monkeypatch.setattr(pipeline, 'poll_news', lambda *args: news(conn, issuer, 'fresh'))
    monkeypatch.setattr(pipeline, 'poll_corporate_rss', lambda *args: None)
    monkeypatch.setattr(pipeline, 'poll_prices', lambda *args: None)
    monkeypatch.setattr(pipeline, 'send_urgent', lambda *args: None)
    monkeypatch.setattr(pipeline, 'analyze_pending', lambda *args: None)
    monkeypatch.setattr(pipeline, 'process_poll_email', lambda _conn, after: observed.append(after))
    pipeline.poll(conn, tmp_path, {'sources': {}, 'http_timeout_seconds': 1, 'http_retries': 0})
    assert observed == [0]
    assert conn.execute('SELECT count(*) FROM news').fetchone()[0] == 1
    conn.close()


def test_cli_queue_and_retry_reject_uncertain(tmp_path, monkeypatch, capsys):
    from bond_watch.cli import main
    conn, issuer = setup_db(tmp_path)
    news(conn, issuer, 'credit')
    enqueue_poll_alerts(conn, 0, config().recipients)
    with conn:
        conn.execute("UPDATE notifications SET status='uncertain',result='delivery_unknown'")
    conn.close()
    root = tmp_path
    (root / 'config').mkdir()
    (root / 'config' / 'settings.yaml').write_text('database: alerts.sqlite\nlog_file: log.txt\nsources: {}\n')
    (root / 'config' / 'portfolio.yaml').write_text('isins: []\n')
    (root / 'config' / 'verified_bonds.yaml').write_text('bonds: {}\n')
    for name, value in {'SMTP_HOST': 'smtp.gmail.com', 'SMTP_PORT': '587',
                        'SMTP_USERNAME': 'user', 'SMTP_PASSWORD': 'secret',
                        'SMTP_FROM': 'user@gmail.com', 'SMTP_TO': 'a@example.com',
                        'SMTP_USE_STARTTLS': 'true'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr('bond_watch.email_notifications.send_email', lambda *args: (_ for _ in ()).throw(AssertionError('must not send')))
    assert main(['--root', str(root), 'email-queue']) == 0
    output = capsys.readouterr().out
    assert 'uncertain' in output and 'secret' not in output
    assert main(['--root', str(root), 'email-retry', '--id', '1']) == 0
    assert "'requeued': 0" in capsys.readouterr().out
    conn = connect(root / 'alerts.sqlite')
    assert conn.execute('SELECT status FROM notifications').fetchone()[0] == 'uncertain'
    conn.close()


def test_systemd_daily_timer_is_moscow_only():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    timer = (root / 'deploy' / 'bond-watch-email-digest.timer').read_text(encoding='utf-8-sig')
    service = (root / 'deploy' / 'bond-watch-email-digest.service').read_text(encoding='utf-8-sig')
    assert 'OnCalendar=*-*-* 19:00:00 Europe/Moscow' in timer
    assert 'Persistent=true' in timer
    assert 'ExecStart=/opt/bond-watch/.venv/bin/bond-watch email-daily' in service


def test_interrupted_auto_attempt_stays_for_manual_review(tmp_path, monkeypatch):
    import pytest
    conn, issuer = setup_db(tmp_path)
    news(conn, issuer, 'credit')
    enqueue_poll_alerts(conn, 0, config().recipients)
    monkeypatch.setattr('bond_watch.email_notifications.send_email', lambda *args: (_ for _ in ()).throw(SystemExit('interrupted')))
    with pytest.raises(SystemExit):
        deliver_due(conn, config())
    assert conn.execute('SELECT status,attempt_count FROM notifications').fetchone()['status'] == 'attempting'
    assert deliver_due(conn, config())['sent'] == 0
    assert retry_safe(conn, config(), 1)['requeued'] == 0
    conn.close()
