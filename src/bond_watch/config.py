from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ISIN_RE = re.compile(r"^RU[0-9A-Z]{10}$")


def project_root() -> Path:
    configured = os.getenv("BOND_WATCH_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    current = Path.cwd().resolve()
    if (current / "config/settings.yaml").is_file() and (current / "config/portfolio.yaml").is_file():
        return current
    return Path(__file__).resolve().parents[2]


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def load_settings(root: Path) -> dict:
    data = yaml.safe_load((root / "config/settings.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("settings.yaml must be a mapping")
    return data


def load_portfolio(root: Path) -> list[str]:
    data = yaml.safe_load((root / "config/portfolio.yaml").read_text(encoding="utf-8"))
    values = data.get("isins") if isinstance(data, dict) else None
    if not isinstance(values, list) or any(not isinstance(x, str) or not ISIN_RE.fullmatch(x) for x in values):
        raise ValueError("portfolio.yaml must contain valid RU ISINs")
    if len(values) != len(set(values)):
        raise ValueError("Duplicate ISIN in portfolio.yaml")
    return values


def save_portfolio(root: Path, isins: list[str]) -> None:
    target = root / "config/portfolio.yaml"
    temp = target.with_suffix(".yaml.tmp")
    temp.write_text(yaml.safe_dump({"isins": isins}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temp.replace(target)




def load_verified_bonds(root: Path) -> dict:
    path = root / "config/verified_bonds.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    bonds = data.get("bonds") if isinstance(data, dict) else None
    if not isinstance(bonds, dict):
        raise ValueError("verified_bonds.yaml must contain bonds mapping")
    for isin, value in bonds.items():
        if not ISIN_RE.fullmatch(isin) or not isinstance(value, dict):
            raise ValueError(f"Invalid verified bond record: {isin}")
        if not value.get("issuer") or not value.get("source_url"):
            raise ValueError(f"Missing issuer or provenance for {isin}")
    return bonds
