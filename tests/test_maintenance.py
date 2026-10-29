"""Housekeeping deletes only what AdProduct created."""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.maintenance import _is_owned_file, cleanup_old_files


def age(path: Path, days: float) -> None:
    old = time.time() - days * 86_400
    os.utime(path, (old, old))


class TestOwnership:
    def test_recognises_its_own_downloads_and_screenshots(self, tmp_path: Path) -> None:
        for name in ("20260918T101530Z_campaign.csv", "20260918T101530Z_download-timeout.png"):
            path = tmp_path / name
            path.write_text("x")
            assert _is_owned_file(path) is True

    def test_does_not_claim_foreign_files(self, tmp_path: Path) -> None:
        for name in ("campaign.csv", "important-backup.csv", "notes.txt", "20260918_report.csv"):
            path = tmp_path / name
            path.write_text("x")
            assert _is_owned_file(path) is False

    def test_does_not_claim_directories(self, tmp_path: Path) -> None:
        directory = tmp_path / "20260918T101530Z_thing.csv"
        directory.mkdir()

        assert _is_owned_file(directory) is False


class TestCleanup:
    def test_removes_expired_owned_files(self, settings) -> None:
        stale = settings.download_dir / "20260101T000000Z_campaign.csv"
        stale.write_text("x")
        age(stale, settings.retention_days + 5)

        assert cleanup_old_files(settings) == 1
        assert not stale.exists()

    def test_keeps_recent_owned_files(self, settings) -> None:
        recent = settings.download_dir / "20260918T101530Z_campaign.csv"
        recent.write_text("x")

        assert cleanup_old_files(settings) == 0
        assert recent.exists()

    def test_never_removes_unrelated_files(self, settings) -> None:
        foreign = settings.download_dir / "customer-export.csv"
        foreign.write_text("precious")
        age(foreign, 3650)

        assert cleanup_old_files(settings) == 0
        assert foreign.read_text() == "precious"

    def test_only_looks_inside_adflow_directories(self, settings, tmp_path: Path) -> None:
        outside = tmp_path / "20260101T000000Z_campaign.csv"
        outside.write_text("x")
        age(outside, 3650)

        cleanup_old_files(settings)

        assert outside.exists()

    def test_sweeps_every_managed_directory(self, settings) -> None:
        for directory in (settings.download_dir, settings.processed_dir,
                          settings.failed_dir, settings.screenshot_dir):
            path = directory / "20260101T000000Z_old.csv"
            path.write_text("x")
            age(path, 999)

        assert cleanup_old_files(settings) == 4
