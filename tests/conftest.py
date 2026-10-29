"""Shared fixtures.

Nothing here touches Amazon or a real MySQL server.  Database tests run
against in-memory SQLite through the same SQLAlchemy code the production
path uses, so the transaction behaviour is exercised for real.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings, load_settings
from app.database import AdvertisedProductRepository
from app.schema import build_bootstrap_table

TABLE = "Advertised_product"

#: Minimum environment a valid Settings needs.
BASE_ENV = {
    "AMAZON_ENTITY_ID": "ENTITY_TEST",
    "DB_HOST": "localhost",
    "DB_PORT": "3306",
    "DB_NAME": "adproduct_test",
    "DB_USER": "tester",
    "DB_PASSWORD": "s3cret",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from the developer's real environment and ``.env``."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *_args, **_kwargs: False)
    prefixes = ("AMAZON_", "DB_", "DOWNLOAD_", "PROCESSED_", "FAILED_", "LOG_", "SCREENSHOT_",
                "SESSION_", "LOCK_", "HEADLESS", "BROWSER_", "VIEWPORT_", "SLOW_MO",
                "REPORT_", "MAX_RETRIES", "RETRY_", "CREATE_TABLE", "FILE_RETENTION",
                "KEEP_PROCESSED")
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Populate the minimum valid environment and return it for mutation."""
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(BASE_ENV)


@pytest.fixture
def settings(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fully valid settings pointed at a temporary directory tree."""
    for key, name in (
        ("DOWNLOAD_DIR", "downloads"),
        ("PROCESSED_DIR", "processed"),
        ("FAILED_DIR", "failed"),
        ("SCREENSHOT_DIR", "screenshots"),
        ("LOG_DIR", "logs"),
        ("SESSION_DIR", "session"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "adproduct.lock"))
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    loaded = load_settings()
    loaded.ensure_directories()
    return loaded


@pytest.fixture
def repository() -> Iterator[AdvertisedProductRepository]:
    """Repository over an in-memory SQLite copy of the table."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata, _table = build_bootstrap_table(TABLE)
    metadata.create_all(engine)
    repo = AdvertisedProductRepository(engine, table=TABLE)
    repo.reflect()
    yield repo
    repo.dispose()


# -- report files, shaped like Amazon's exports ------------------------------


def sp_frame(date: str = "2026-10-29") -> pd.DataFrame:
    """Sponsored Products Advertised product (XLSX: real dates and numbers)."""
    return pd.DataFrame(
        {
            "Date": [pd.Timestamp(date), pd.Timestamp(date)],
            "Portfolio name": ["P", "P"],
            "Currency": ["USD", "USD"],
            "Campaign Name": ["SP - Auto", "SP - Exact"],
            "Ad Group Name": ["AG", "AG"],
            "Advertised SKU": ["SKU1", "SKU2"],
            "Advertised ASIN": ["B000000001", "B000000002"],
            "Impressions": [100, 50],
            "7 Day Total Sales ": [120.5, 0.0],
            "7 Day Total Units (#)": [3, 0],
        }
    )


def sb_csv(date: str = "Oct 29, 2026") -> str:
    """Sponsored Brands Attributed Purchases (CSV: currency text)."""
    return (
        "Date,Currency,Campaign Name,Attribution type,Purchased ASIN,"
        "14 Day Total Sales ,14 Day Total Units (#)\n"
        f'"{date}",USD,SB - Brand,Promoted,B000000003,"$1,204.55",4\n'
        f'"{date}",USD,SB - Brand,Brand Halo,B000000004,$15.00,1\n'
    )


def sd_frame(date: str = "2026-10-29") -> pd.DataFrame:
    """Sponsored Display Advertised product (XLSX)."""
    return pd.DataFrame(
        {
            "Date": [pd.Timestamp(date)],
            "Currency": ["USD"],
            "Campaign Name": ["SD - Retarget"],
            "Advertised ASIN": ["B000000005"],
            "14 Day Total Sales ": [30.0],
            "14 Day Total Units (#)": [2],
        }
    )


@pytest.fixture
def report_files(tmp_path: Path) -> dict[str, Path]:
    """One file per report, in the formats Amazon delivers them."""
    folder = tmp_path / "incoming"
    folder.mkdir()
    sp = folder / "20261029T090000Z_sp_Sponsored_Products_Advertised_product_report.xlsx"
    sb = folder / "20261029T090000Z_sb_Sponsored_Brands_Attributed_Purchases_report.csv"
    sd = folder / "20261029T090000Z_sd_Sponsored_Display_Advertised_product_report.xlsx"
    sp_frame().to_excel(sp, index=False)
    sb.write_text(sb_csv(), encoding="utf-8")
    sd_frame().to_excel(sd, index=False)
    return {"sp": sp, "sb": sb, "sd": sd}
