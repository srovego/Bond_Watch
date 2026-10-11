from bond_watch.config import load_settings, load_verified_bonds, project_root
from bond_watch.db import connect
from bond_watch.pipeline import init_sources, seed_verified_bonds, sync_portfolio


def test_unlicensed_rating_sources_remain_unavailable_and_create_no_events(tmp_path):
    root = project_root()
    settings = load_settings(root)
    assert settings["sources"]["expert_ra"] is False
    assert settings["sources"]["acra"] is False
    conn = connect(tmp_path / "rating_sources.sqlite")
    init_sources(conn, settings)
    sync_portfolio(conn, root)
    seed_verified_bonds(conn, root)
    rows = conn.execute("SELECT id,enabled,status,last_error FROM sources WHERE id IN ('expert_ra','acra') ORDER BY id").fetchall()
    assert [(r["id"], r["enabled"], r["status"]) for r in rows] == [
        ("acra", 0, "unavailable"), ("expert_ra", 0, "unavailable")
    ]
    assert all(r["last_error"] for r in rows)
    assert conn.execute("SELECT count(*) FROM collector_runs WHERE source_id IN ('expert_ra','acra')").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM news WHERE source_id IN ('expert_ra','acra')").fetchone()[0] == 0
    corporate = conn.execute("SELECT DISTINCT i.inn FROM issuers i JOIN bonds b ON b.issuer_id=i.id WHERE i.category='corporate'").fetchall()
    assert len(corporate) == 7
    assert all(r["inn"] for r in corporate)
    conn.close()


def test_existing_rating_links_do_not_prove_live_rating_coverage():
    bonds = load_verified_bonds(project_root())
    corporate = [item for item in bonds.values() if item["category"] == "corporate"]
    assert len(corporate) == 7
    assert sum(bool(item.get("rating_url")) for item in corporate) == 6
    assert all("rating_url" not in item or item["rating_url"].startswith("https://") for item in corporate)
