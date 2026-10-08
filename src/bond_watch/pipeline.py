from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .analysis import ai_analyze, classify, fingerprint, match_issuers
from .config import load_portfolio, load_verified_bonds
from .db import utcnow
from .http import Http
from .sources import Publication, fetch_bond_price, fetch_cbr_rss, resolve_isin
from .telegram import Telegram, send_once, format_news

log = logging.getLogger(__name__)

SOURCE_NAMES = {
    "moex_iss": "Московская биржа ISS",
    "cbr_rss": "Банк России RSS",
    "e_disclosure": "e-disclosure",
    "expert_ra": "Эксперт РА",
    "acra": "АКРА",
    "fedresurs": "Федресурс",
}
SOURCE_REASONS = {
    "e_disclosure": "официальный API требует платной авторизации",
    "expert_ra": "официальная автоматическая выгрузка доступна по подписке",
    "acra": "публичный стабильный API/RSS для мониторинга не подтвержден",
    "fedresurs": "официальный API требует авторизации",
}


def init_sources(conn, settings: dict) -> None:
    with conn:
        for source_id, name in SOURCE_NAMES.items():
            enabled = int(bool(settings.get("sources", {}).get(source_id, False)))
            status = "never_checked" if enabled else "unavailable"
            reason = None if enabled else SOURCE_REASONS.get(source_id, "отключен")
            conn.execute(
                """INSERT INTO sources(id,name,enabled,status,last_error) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,enabled=excluded.enabled,
                status=CASE WHEN excluded.enabled=0 THEN 'unavailable' ELSE sources.status END,
                last_error=CASE WHEN excluded.enabled=0 THEN excluded.last_error ELSE sources.last_error END""",
                (source_id, name, enabled, status, reason),
            )


def sync_portfolio(conn, root: Path) -> None:
    isins = load_portfolio(root)
    with conn:
        for isin in isins:
            conn.execute("INSERT OR IGNORE INTO bonds(isin) VALUES(?)", (isin,))
        stale = conn.execute("SELECT isin FROM bonds").fetchall()
        for row in stale:
            if row["isin"] not in isins:
                conn.execute("DELETE FROM price_observations WHERE isin=?", (row["isin"],))
                conn.execute("DELETE FROM bonds WHERE isin=?", (row["isin"],))


def seed_verified_bonds(conn, root: Path) -> None:
    reference = load_verified_bonds(root)
    for isin in load_portfolio(root):
        data = reference.get(isin)
        if not data:
            continue
        with conn:
            bond = conn.execute("SELECT status FROM bonds WHERE isin=?", (isin,)).fetchone()
            if not bond:
                continue
            if bond["status"] != "verified":
                issuer = conn.execute("SELECT id FROM issuers WHERE inn=?", (data.get("inn"),)).fetchone()
                if issuer:
                    issuer_id = issuer["id"]
                else:
                    issuer_id = conn.execute(
                        "INSERT INTO issuers(name,inn,category) VALUES(?,?,?)",
                        (data["issuer"], data.get("inn"), data["category"]),
                    ).lastrowid
                conn.execute(
                    """UPDATE bonds SET secid=?,issuer_id=?,issue_name=?,issue_number=?,bond_type=?,
                    card_url=?,status='verified',last_resolved_at=?,resolution_error=NULL WHERE isin=?""",
                    (data["secid"], issuer_id, data["issue_name"], data["issue_number"],
                     data["bond_type"], data.get("card_url") or data["source_url"], utcnow(), isin),
                )
            if data.get("rating"):
                conn.execute(
                    """UPDATE bonds SET rating=?,rating_agency=?,rating_date=?,rating_url=?,rating_status=?
                    WHERE isin=? AND (rating_date IS NULL OR rating_date<=?)""",
                    (data["rating"], data["rating_agency"], str(data["rating_date"]), data["rating_url"],
                     data.get("rating_status"), isin, str(data["rating_date"])),
                )

def _run(conn, source_id: str, action, diagnostics: dict[str, int] | None = None):
    started = utcnow()
    with conn:
        cur = conn.execute("INSERT INTO collector_runs(source_id,started_at,status) VALUES(?,?,?)", (source_id, started, "running"))
    try:
        count = action()
    except Exception as exc:
        log.exception("%s collection failed", source_id)
        with conn:
            conn.execute(
                "UPDATE collector_runs SET finished_at=?,status='error',error=?,items_seen=?,rejection_counts_json=? WHERE id=?",
                (utcnow(), str(exc)[:500], (diagnostics or {}).get("accepted", 0),
                 json.dumps(diagnostics, ensure_ascii=False) if diagnostics is not None else None, cur.lastrowid),
            )
            conn.execute("UPDATE sources SET status='error',last_error=? WHERE id=?", (str(exc)[:500], source_id))
        return False
    with conn:
        conn.execute(
            "UPDATE collector_runs SET finished_at=?,status='ok',items_seen=?,rejection_counts_json=? WHERE id=?",
            (utcnow(), count, json.dumps(diagnostics, ensure_ascii=False) if diagnostics is not None else None, cur.lastrowid),
        )
        conn.execute("UPDATE sources SET status='ok',last_success_at=?,last_error=NULL WHERE id=?", (utcnow(), source_id))
    return True


def resolve_portfolio(conn, root: Path, settings: dict, http: Http) -> None:
    sync_portfolio(conn, root)
    def action():
        seen = 0
        errors = []
        for row in conn.execute("SELECT isin,secid FROM bonds ORDER BY isin").fetchall():
            isin = row["isin"]
            try:
                data = resolve_isin(http, isin, row["secid"])
                with conn:
                    issuer = None
                    if data["inn"]:
                        issuer = conn.execute("SELECT id FROM issuers WHERE inn=?", (data["inn"],)).fetchone()
                    if not issuer and data["moex_id"]:
                        issuer = conn.execute("SELECT id FROM issuers WHERE moex_id=?", (data["moex_id"],)).fetchone()
                    if not issuer:
                        issuer = conn.execute("SELECT id FROM issuers WHERE name=? AND category=?", (data["issuer_name"], data["category"])).fetchone()
                    if issuer:
                        issuer_id = issuer["id"]
                        conn.execute("UPDATE issuers SET inn=COALESCE(inn,?),moex_id=COALESCE(moex_id,?) WHERE id=?", (data["inn"], data["moex_id"], issuer_id))
                    else:
                        issuer_id = conn.execute(
                            "INSERT INTO issuers(name,inn,moex_id,category) VALUES(?,?,?,?)",
                            (data["issuer_name"], data["inn"], data["moex_id"], data["category"]),
                        ).lastrowid
                    conn.execute(
                        """UPDATE bonds SET secid=?,issuer_id=?,shortname=?,issue_name=?,issue_number=?,
                        bond_type=?,card_url=?,nominal=?,status='verified',last_resolved_at=?,resolution_error=NULL
                        WHERE isin=?""",
                        (data["secid"], issuer_id, data["shortname"], data["issue_name"], data["issue_number"],
                         data["bond_type"], data["card_url"], data["nominal"], utcnow(), isin),
                    )
                seen += 1
            except Exception as exc:
                errors.append(f"{isin}: {exc}")
                with conn:
                    conn.execute(
                        "UPDATE bonds SET status=CASE WHEN status='verified' THEN status ELSE 'unresolved' END,last_resolved_at=?,resolution_error=? WHERE isin=?",
                        (utcnow(), str(exc)[:300], isin),
                    )
            time.sleep(float(settings.get("request_pause_seconds", 0.4)))
        if errors:
            raise RuntimeError("; ".join(errors)[:500])
        return seen
    _run(conn, "moex_iss", action)


def _issuer_rows(conn) -> list[dict]:
    return [dict(r) | {"isins": [b["isin"] for b in conn.execute("SELECT isin FROM bonds WHERE issuer_id=?", (r["id"],))]} for r in conn.execute("SELECT id,name,inn,category FROM issuers WHERE status='verified'")]


def ingest_publication(conn, item: Publication) -> int | None:
    issuer_ids = match_issuers(item, _issuer_rows(conn))
    if not issuer_ids:
        return None
    category, severity = classify(item)
    fp = fingerprint(item) + ":" + ",".join(map(str, sorted(issuer_ids)))
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO news(source_id,source_item_id,url,source_name,published_at,discovered_at,title,body,category,severity,fingerprint)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (item.source_id, item.item_id, item.url, item.source_name, item.published_at, utcnow(),
             item.title, item.body, category, severity, fp),
        )
        row = conn.execute("SELECT id FROM news WHERE source_id=? AND source_item_id=?", (item.source_id, item.item_id)).fetchone()
        news_id = row["id"]
        for issuer_id in issuer_ids:
            conn.execute("INSERT OR IGNORE INTO news_issuers(news_id,issuer_id) VALUES(?,?)", (news_id, issuer_id))
            bonds = conn.execute("SELECT isin FROM bonds WHERE issuer_id=?", (issuer_id,)).fetchall()
            mentioned = [b["isin"] for b in bonds if b["isin"] in (item.title + " " + item.body).upper()]
            for isin in (mentioned or [b["isin"] for b in bonds]):
                conn.execute("INSERT OR IGNORE INTO news_bonds(news_id,isin) VALUES(?,?)", (news_id, isin))
    return news_id if cur.rowcount else None


def analyze_pending(conn) -> None:
    if os.getenv("AI_ENABLED", "false").lower() != "true":
        with conn:
            conn.execute("UPDATE news SET analysis_status='disabled' WHERE analysis_status='pending'")
        return
    rows = conn.execute(
        """SELECT n.*, i.name issuer_name FROM news n JOIN news_issuers ni ON ni.news_id=n.id
        JOIN issuers i ON i.id=ni.issuer_id WHERE n.analysis_status IN ('pending','error','disabled')
        GROUP BY n.id ORDER BY n.id LIMIT 20"""
    ).fetchall()
    for row in rows:
        item = Publication(row["source_id"], row["source_item_id"], row["url"], row["source_name"], row["published_at"], row["title"], row["body"] or "")
        try:
            result = ai_analyze(item, row["issuer_name"])
            with conn:
                conn.execute(
                    """INSERT INTO analysis(news_id,summary,facts_json,impact,confidence,model,analyzed_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(news_id) DO UPDATE SET
                    summary=excluded.summary,facts_json=excluded.facts_json,impact=excluded.impact,
                    confidence=excluded.confidence,model=excluded.model,analyzed_at=excluded.analyzed_at,error=NULL""",
                    (row["id"], str(result.get("summary", ""))[:1000], json.dumps(result["facts"], ensure_ascii=False),
                     str(result.get("impact", ""))[:1000], result["confidence"], os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), utcnow()),
                )
                conn.execute("UPDATE news SET analysis_status='done' WHERE id=?", (row["id"],))
        except Exception as exc:
            log.exception("AI analysis failed for news %s", row["id"])
            with conn:
                conn.execute("INSERT OR REPLACE INTO analysis(news_id,error,analyzed_at) VALUES(?,?,?)", (row["id"], str(exc)[:500], utcnow()))
                conn.execute("UPDATE news SET analysis_status='error' WHERE id=?", (row["id"],))


def poll_news(conn, settings: dict, http: Http) -> None:
    if not settings.get("sources", {}).get("cbr_rss", True):
        return
    def action():
        publications = fetch_cbr_rss(http, lookback_days=int(settings.get("news_lookback_days", 3)))
        for item in publications:
            ingest_publication(conn, item)
        return len(publications)
    _run(conn, "cbr_rss", action)


def _price_event(conn, bond, current: dict, previous, change: float) -> None:
    direction = "выросла" if change > 0 else "снизилась"
    title = f"Чистая цена {bond['isin']} {direction} на {abs(change):.2f}%"
    cbr = conn.execute("SELECT status FROM sources WHERE id='cbr_rss'").fetchone()
    publication_note = (
        "RSS Банка России проверена в текущем цикле."
        if cbr and cbr["status"] == "ok"
        else "Публикации Банка России проверить не удалось."
    )
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    related = conn.execute(
        """SELECT n.title,n.url FROM news n JOIN news_issuers ni ON ni.news_id=n.id
        WHERE ni.issuer_id=? AND n.category!='price_move' AND n.discovered_at>=?
        ORDER BY n.discovered_at DESC LIMIT 2""",
        (bond["issuer_id"], recent_cutoff),
    ).fetchall()
    related_note = (
        "Связанные публикации: " + "; ".join(f"{r['title']} ({r['url']})" for r in related)
        if related else "Связанных публикаций эмитента за 3 дня в базе не найдено."
    )
    body = (
        f"MOEX ISS: предыдущее закрытие {previous['clean_price']:.2f}% номинала; "
        f"{current['trading_date']} — {current['clean_price']:.2f}% номинала. "
        f"{publication_note} {related_note} Изменение цены само по себе не подтверждает кредитное событие."
    )
    key = f"price:{bond['isin']}:{current['trading_date']}"
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO news(source_id,source_item_id,url,source_name,published_at,discovered_at,
            title,body,category,severity,fingerprint,analysis_status)
            VALUES('moex_iss',?,?,?,?,?,?,?,?,?,?, 'disabled')""",
            (key, bond["card_url"] or "", "Московская биржа ISS", utcnow(), utcnow(),
             title, body, "price_move", "WARNING", key),
        )
        if cur.rowcount:
            news_id = cur.lastrowid
            conn.execute("INSERT INTO news_issuers(news_id,issuer_id) VALUES(?,?)", (news_id, bond["issuer_id"]))
            conn.execute("INSERT INTO news_bonds(news_id,isin) VALUES(?,?)", (news_id, bond["isin"]))

def poll_prices(conn, settings: dict, http: Http) -> None:
    if not settings.get("sources", {}).get("moex_iss", True):
        return
    diagnostics: dict[str, int] = {}

    def count_reason(reason: str) -> None:
        diagnostics[reason] = diagnostics.get(reason, 0) + 1

    def action():
        count = 0
        errors = []
        rows = conn.execute(
            """SELECT b.*,i.category FROM bonds b JOIN issuers i ON i.id=b.issuer_id
            WHERE b.status='verified' AND b.secid IS NOT NULL"""
        ).fetchall()
        if not rows:
            raise RuntimeError("no verified bonds; run bond-watch resolve")
        for bond in rows:
            try:
                current = fetch_bond_price(http, bond["secid"], diagnostics)
                if not current:
                    count_reason("no_valid_quote")
                    continue
                previous = conn.execute(
                    """SELECT * FROM price_observations WHERE isin=? AND trading_date<?
                    ORDER BY trading_date DESC LIMIT 1""", (bond["isin"], current["trading_date"])
                ).fetchone()
                if not previous:
                    count_reason("comparison_no_prior_observation")
                elif not current["previous_trade_date"] or previous["trading_date"] != current["previous_trade_date"]:
                    count_reason("comparison_prior_date_mismatch")
                elif previous["nominal"] != current["nominal"]:
                    count_reason("comparison_nominal_changed")
                elif current["previous_clean_price"] is None:
                    count_reason("comparison_no_previous_close")
                else:
                    change = 100 * (current["clean_price"] / current["previous_clean_price"] - 1)
                    threshold_key = "price_threshold_ofz_percent" if bond["category"] == "ofz" else "price_threshold_corporate_percent"
                    if abs(change) >= float(settings[threshold_key]):
                        _price_event(conn, bond, current, {"clean_price": current["previous_clean_price"]}, change)
                with conn:
                    conn.execute(
                        """INSERT INTO price_observations(isin,trading_date,clean_price,nominal,observed_at)
                        VALUES(?,?,?,?,?) ON CONFLICT(isin,trading_date) DO UPDATE SET
                        clean_price=excluded.clean_price,nominal=excluded.nominal,observed_at=excluded.observed_at""",
                        (bond["isin"], current["trading_date"], current["clean_price"], current["nominal"], utcnow()),
                    )
                count += 1
                diagnostics["accepted"] = count
            except Exception as exc:
                errors.append(f"{bond['isin']}: {exc}")
                count_reason("processing_error")
            finally:
                time.sleep(float(settings.get("request_pause_seconds", 0.4)))
        log.info("MOEX price poll: accepted=%s diagnostics=%s", count, diagnostics)
        if errors:
            raise RuntimeError("; ".join(errors)[:500])
        return count
    _run(conn, "moex_iss", action, diagnostics)


def send_urgent(conn) -> None:
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        return
    rows = conn.execute(
        """SELECT n.*,GROUP_CONCAT(DISTINCT i.name) issuer_name,GROUP_CONCAT(DISTINCT b.isin) isins
        FROM news n JOIN news_issuers ni ON ni.news_id=n.id JOIN issuers i ON i.id=ni.issuer_id
        LEFT JOIN news_bonds nb ON nb.news_id=n.id
        LEFT JOIN bonds b ON b.isin=nb.isin
        WHERE n.severity='CRITICAL' AND n.source_id='cbr_rss'
        AND n.id=(SELECT MIN(x.id) FROM news x WHERE x.fingerprint=n.fingerprint)
        GROUP BY n.id ORDER BY n.id"""
    ).fetchall()
    if not rows:
        return
    telegram = Telegram()
    for row in rows:
        try:
            send_once(conn, telegram, chat_id, format_news(row), f"urgent:{row['fingerprint']}", news_id=row["id"], kind="urgent")
        except Exception:
            log.exception("Urgent notification failed for %s", row["id"])


def poll(conn, root: Path, settings: dict) -> None:
    init_sources(conn, settings)
    sync_portfolio(conn, root)
    seed_verified_bonds(conn, root)
    with Http(float(settings.get("http_timeout_seconds", 12)), int(settings.get("http_retries", 2))) as http:
        poll_news(conn, settings, http)
        poll_prices(conn, settings, http)
    send_urgent(conn)
    analyze_pending(conn)















