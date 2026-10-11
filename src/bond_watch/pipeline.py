from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .analysis import ai_analyze, classify, fingerprint, match_issuers
from .config import load_portfolio, load_verified_bonds
from .db import utcnow
from .http import Http
from .news_sources import classify_event, fetch_cbr_site_rss, fetch_moex_news, is_market_event, match_portfolio
from .sources import Publication, fetch_bond_price, fetch_cbr_rss, resolve_isin
from .telegram import Telegram, send_once, format_news

log = logging.getLogger(__name__)

SOURCE_NAMES = {
    "moex_iss": "Московская биржа ISS",
    "cbr_rss": "Банк России RSS",
    "cbr_site_rss": "Банк России: новое на сайте",
    "moex_news": "Московская биржа: новости RSS",
    "e_disclosure": "e-disclosure",
    "expert_ra": "Эксперт РА",
    "acra": "АКРА",
    "fedresurs": "Федресурс",
}
SOURCE_REASONS = {
    "e_disclosure": "официальный API требует платной авторизации",
    "expert_ra": "автоматическая выгрузка рейтинговых действий требует подписки Эксперт РА",
    "acra": "публичный API/RSS не подтвержден; использование материалов в новостных сводках требует согласия АКРА",
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

def _run(conn, source_id: str, action, diagnostics: dict[str, int] | None = None, *, pass_run_id: bool = False):
    started = utcnow()
    with conn:
        cur = conn.execute("INSERT INTO collector_runs(source_id,started_at,status) VALUES(?,?,?)", (source_id, started, "running"))
    try:
        count = action(cur.lastrowid) if pass_run_id else action()
    except Exception as exc:
        log.exception("%s collection failed", source_id)
        with conn:
            conn.execute(
                "UPDATE collector_runs SET finished_at=?,status='error',error=?,items_seen=?,rejection_counts_json=? WHERE id=?",
                (utcnow(), str(exc)[:500], (diagnostics or {}).get("received", (diagnostics or {}).get("accepted", 0)),
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


def ingest_publication(conn, item: Publication, issuer_ids: list[int] | None = None) -> int | None:
    issuer_ids = match_issuers(item, _issuer_rows(conn)) if issuer_ids is None else issuer_ids
    if len(issuer_ids) != 1:
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
    diagnostics = {"received": 0, "already_known": 0, "no_issuer_match": 0,
                   "ambiguous_issuer_match": 0, "saved_new": 0}

    def action(run_id: int):
        publications = fetch_cbr_rss(http, lookback_days=int(settings.get("news_lookback_days", 3)))
        issuers = _issuer_rows(conn)
        for item in publications:
            diagnostics["received"] += 1
            existing = conn.execute(
                "SELECT id FROM news WHERE source_id=? AND source_item_id=?",
                (item.source_id, item.item_id),
            ).fetchone()
            issuer_ids: list[int] = []
            if existing:
                decision = "already_known"
            else:
                issuer_ids = match_issuers(item, issuers)
                if not issuer_ids:
                    decision = "no_issuer_match"
                elif len(issuer_ids) > 1:
                    decision = "ambiguous_issuer_match"
                elif ingest_publication(conn, item, issuer_ids) is not None:
                    decision = "saved_new"
                else:
                    decision = "already_known"
            diagnostics[decision] += 1
            with conn:
                conn.execute(
                    """INSERT INTO rss_item_checks(run_id,source_item_id,title,url,published_at,decision,match_ids_json)
                    VALUES(?,?,?,?,?,?,?)""",
                    (run_id, item.item_id[:500], item.title[:500], item.url[:1000], item.published_at,
                     decision, json.dumps(issuer_ids)),
                )
        with conn:
            conn.execute(
                """DELETE FROM rss_item_checks WHERE run_id NOT IN
                (SELECT id FROM collector_runs WHERE source_id='cbr_rss' ORDER BY id DESC LIMIT 100)"""
            )
        return diagnostics["received"]
    _run(conn, "cbr_rss", action, diagnostics, pass_run_id=True)


def ingest_corporate_event(conn, item: Publication, issuer_id: int | None, bond_isins: list[str], scope: str) -> int | None:
    category, severity = classify_event(item)
    body = (item.body or item.title)[:800]
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO news(source_id,source_item_id,url,source_name,published_at,discovered_at,
            title,body,category,severity,fingerprint,analysis_status,event_scope)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'disabled',?)""",
            (item.source_id, item.item_id, item.url, item.source_name, item.published_at, utcnow(),
             item.title, body, category, severity, fingerprint(item) + f":{issuer_id if issuer_id is not None else scope}", scope),
        )
        if not cur.rowcount:
            return None
        news_id = cur.lastrowid
        if issuer_id is not None:
            conn.execute("INSERT INTO news_issuers(news_id,issuer_id) VALUES(?,?)", (news_id, issuer_id))
        for isin in bond_isins:
            conn.execute("INSERT INTO news_bonds(news_id,isin) VALUES(?,?)", (news_id, isin))
    return news_id


def poll_corporate_rss(conn, settings: dict, http: Http, source_id: str) -> None:
    if not settings.get("sources", {}).get(source_id, False):
        return
    fetcher = {"cbr_site_rss": fetch_cbr_site_rss, "moex_news": fetch_moex_news}[source_id]
    diagnostics = {"received": 0, "matched": 0, "saved": 0, "rejected": 0,
                   "errors": 0, "already_known": 0, "no_portfolio_match": 0,
                   "ambiguous": 0, "market_events": 0}

    def action(run_id: int):
        try:
            items, invalid = fetcher(http, lookback_days=int(settings.get("news_lookback_days", 3)))
        except Exception:
            diagnostics["errors"] += 1
            raise
        diagnostics["errors"] += invalid
        diagnostics["rejected"] += invalid
        issuers = _issuer_rows(conn)
        for item in items:
            diagnostics["received"] += 1
            issuer_id, bond_isins, scope, basis = match_portfolio(item, issuers)
            if issuer_id is None and scope == "market" and is_market_event(item):
                diagnostics["market_events"] += 1
                if ingest_corporate_event(conn, item, None, [], "market") is None:
                    decision = "already_known"
                    diagnostics[decision] += 1
                else:
                    decision = "saved"
                    diagnostics[decision] += 1
            elif issuer_id is None:
                decision = "ambiguous" if scope == "ambiguous" else "no_portfolio_match"
                diagnostics[decision] += 1
                diagnostics["rejected"] += 1
            else:
                diagnostics["matched"] += 1
                if ingest_corporate_event(conn, item, issuer_id, bond_isins, scope) is None:
                    decision = "already_known"
                    diagnostics[decision] += 1
                else:
                    decision = "saved"
                    diagnostics[decision] += 1
            with conn:
                conn.execute(
                    """INSERT INTO rss_item_checks(run_id,source_item_id,title,url,published_at,decision,match_ids_json)
                    VALUES(?,?,?,?,?,?,?)""",
                    (run_id, item.item_id, item.title, item.url, item.published_at,
                     f"{decision}:{basis}", json.dumps([issuer_id] if issuer_id else [])),
                )
        with conn:
            conn.execute(
                """DELETE FROM rss_item_checks WHERE run_id IN
                (SELECT id FROM collector_runs WHERE source_id=? ORDER BY id DESC LIMIT -1 OFFSET 100)""",
                (source_id,),
            )
        return diagnostics["received"]
    _run(conn, source_id, action, diagnostics, pass_run_id=True)


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
                if current["trading_date"] > datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat():
                    count_reason("future_trade_date")
                    continue
                latest = conn.execute(
                    "SELECT * FROM price_observations WHERE isin=? ORDER BY trading_date DESC LIMIT 1",
                    (bond["isin"],),
                ).fetchone()
                if latest and current["trading_date"] < latest["trading_date"]:
                    count_reason("older_than_latest_observation")
                    continue
                if latest and current["trading_date"] == latest["trading_date"]:
                    if not current["trade_moment"] or not latest["trade_moment"] or current["trade_moment"] <= latest["trade_moment"]:
                        count_reason("same_day_quote_not_newer")
                        continue
                previous = conn.execute(
                    """SELECT * FROM price_observations WHERE isin=? AND trading_date<?
                    ORDER BY trading_date DESC LIMIT 1""", (bond["isin"], current["trading_date"])
                ).fetchone()
                if not previous:
                    count_reason("comparison_no_prior_observation")
                elif not current["previous_trade_date"] or previous["trading_date"] != current["previous_trade_date"]:
                    count_reason("comparison_prior_date_mismatch")
                elif not previous["trade_moment"]:
                    count_reason("comparison_unverified_prior_observation")
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
                        """INSERT INTO price_observations(isin,trading_date,clean_price,nominal,observed_at,trade_moment)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(isin,trading_date) DO UPDATE SET
                        clean_price=excluded.clean_price,nominal=excluded.nominal,
                        observed_at=excluded.observed_at,trade_moment=excluded.trade_moment""",
                        (bond["isin"], current["trading_date"], current["clean_price"], current["nominal"],
                         utcnow(), current["trade_moment"]),
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
        poll_corporate_rss(conn, settings, http, "cbr_site_rss")
        poll_corporate_rss(conn, settings, http, "moex_news")
        poll_prices(conn, settings, http)
    send_urgent(conn)
    analyze_pending(conn)















