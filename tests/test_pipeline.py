"""End to end, minus the browser: files -> pandas -> SQLite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.database import AdvertisedProductRepository
from app.downloader import DownloadResult
from app.exceptions import EmptyReportError, ReportValidationError
from app.main import AdProductPipeline, RunMode, RunOptions
from app.schema import Period


def pipeline(
    settings: Settings, repository: AdvertisedProductRepository, **options
) -> AdProductPipeline:
    return AdProductPipeline(
        settings, RunOptions(**options), repository_factory=lambda: repository
    )


def fake_downloads(files: dict[str, Path], settings: Settings):
    """Stand-in for the browser stage: 'download' the given files."""

    def download(self: AdProductPipeline) -> dict[str, DownloadResult]:
        results = {}
        for key, path in files.items():
            target = settings.download_dir / path.name
            shutil.copy(path, target)
            results[key] = DownloadResult(key, f"name-{key}", target, target.stat().st_size, path.name)
        return results

    return download


class TestProcessFiles:
    def test_loads_all_three_reports(
        self, settings, repository, report_files: dict[str, Path]
    ) -> None:
        summary = pipeline(
            settings, repository, mode=RunMode.PROCESS_FILES, files=list(report_files.values())
        ).run()

        assert summary.succeeded
        assert summary.write_result.inserted == 5
        assert repository.count_for_date("2026-10-29") == 5

    def test_file_order_does_not_matter(self, settings, repository, report_files) -> None:
        files = [report_files["sd"], report_files["sb"], report_files["sp"]]
        pipeline(settings, repository, mode=RunMode.PROCESS_FILES, files=files).run()
        assert repository.count_for_date("2026-10-29") == 5

    def test_refuses_a_partial_set(self, settings, repository, report_files) -> None:
        with pytest.raises(ReportValidationError, match="Sponsored Display"):
            pipeline(
                settings,
                repository,
                mode=RunMode.PROCESS_FILES,
                files=[report_files["sp"], report_files["sb"]],
            ).run()
        assert repository.count_for_date("2026-10-29") == 0

    def test_dry_run_writes_nothing(self, settings, repository, report_files) -> None:
        pipeline(
            settings,
            repository,
            mode=RunMode.PROCESS_FILES,
            files=list(report_files.values()),
            dry_run=True,
        ).run()
        assert repository.count_for_date("2026-10-29") == 0


class TestFullRun:
    def test_downloads_are_loaded_then_deleted(
        self, settings, repository, report_files, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            AdProductPipeline, "_download_with_retry", fake_downloads(report_files, settings)
        )
        summary = pipeline(settings, repository, period=Period.TODAY).run()

        assert summary.write_result.inserted == 5
        assert list(settings.download_dir.iterdir()) == []

    def test_wrong_report_type_is_caught(
        self, settings, repository, report_files, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        swapped = {"sp": report_files["sd"], "sb": report_files["sb"], "sd": report_files["sp"]}
        monkeypatch.setattr(
            AdProductPipeline, "_download_with_retry", fake_downloads(swapped, settings)
        )
        with pytest.raises(ReportValidationError, match="requested as"):
            pipeline(settings, repository).run()
        assert repository.count_for_date("2026-10-29") == 0

    def test_all_empty_writes_nothing(
        self, settings, repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = {}
        for key in ("sp", "sb", "sd"):
            path = tmp_path / f"20261029T000000Z_{key}.csv"
            path.write_text("Date,Currency\n")
            empty[key] = path
        monkeypatch.setattr(
            AdProductPipeline, "_download_with_retry", fake_downloads(empty, settings)
        )
        with pytest.raises(EmptyReportError):
            pipeline(settings, repository).run()
