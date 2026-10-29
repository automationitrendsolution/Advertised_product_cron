"""Report reading and mapping: the original upload script's rules."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.exceptions import ReportValidationError
from app.processor import (
    clean_number,
    combine,
    identify_report,
    read_report_file,
    to_records,
    transform_report,
)
from app.schema import OUTPUT_COLUMNS, SPONSORED_BRANDS, SPONSORED_DISPLAY, SPONSORED_PRODUCTS
from tests.conftest import sb_csv, sd_frame, sp_frame


class TestReading:
    def test_reads_xlsx_and_csv(self, report_files: dict[str, Path]) -> None:
        assert len(read_report_file(report_files["sp"])) == 2
        assert len(read_report_file(report_files["sb"])) == 2

    def test_rejects_an_html_page(self, tmp_path: Path) -> None:
        page = tmp_path / "report.csv"
        page.write_text("<!DOCTYPE html><html>Sign in</html>")
        with pytest.raises(ReportValidationError, match="HTML page"):
            read_report_file(page)

    def test_rejects_an_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "report.csv"
        empty.write_text("   ")
        with pytest.raises(ReportValidationError, match="empty"):
            read_report_file(empty)

    def test_drops_blank_excel_rows(self, tmp_path: Path) -> None:
        frame = pd.concat([sd_frame(), pd.DataFrame([{}])], ignore_index=True)
        path = tmp_path / "sd.xlsx"
        frame.to_excel(path, index=False)
        assert len(read_report_file(path)) == 1


class TestIdentify:
    def test_tells_the_three_reports_apart(self, report_files: dict[str, Path]) -> None:
        assert identify_report(read_report_file(report_files["sp"])) is SPONSORED_PRODUCTS
        assert identify_report(read_report_file(report_files["sb"])) is SPONSORED_BRANDS
        assert identify_report(read_report_file(report_files["sd"])) is SPONSORED_DISPLAY

    def test_rejects_an_unrelated_report(self) -> None:
        with pytest.raises(ReportValidationError, match="not one of the three"):
            identify_report(pd.DataFrame({"Search term": ["x"]}))


class TestTransform:
    def test_sponsored_products_mapping(self, tmp_path: Path) -> None:
        out = transform_report(sp_frame(), SPONSORED_PRODUCTS, source=tmp_path / "sp.xlsx").frame

        assert list(out.columns) == list(OUTPUT_COLUMNS)
        assert out["ASIN"].tolist() == ["B000000001", "B000000002"]
        assert out["Sales"].tolist() == [120.5, 0.0]
        assert out["Units"].tolist() == [3, 0]
        assert out["Date"].tolist() == ["2026-10-29", "2026-10-29"]
        assert out["Attribution_type"].isna().all()

    def test_sponsored_brands_cleans_currency_and_keeps_attribution(
        self, report_files: dict[str, Path]
    ) -> None:
        frame = read_report_file(report_files["sb"])
        out = transform_report(frame, SPONSORED_BRANDS, source=report_files["sb"]).frame

        assert out["Sales"].tolist() == [1204.55, 15.0]
        assert out["Attribution_type"].tolist() == ["Promoted", "Brand Halo"]
        assert out["ASIN"].tolist() == ["B000000003", "B000000004"]
        assert out["Date"].tolist() == ["2026-10-29", "2026-10-29"]

    def test_header_whitespace_and_case_do_not_matter(self, tmp_path: Path) -> None:
        frame = sd_frame().rename(columns={"14 Day Total Sales ": "14 day total  sales"})
        out = transform_report(frame, SPONSORED_DISPLAY, source=tmp_path / "sd.xlsx").frame
        assert out["Sales"].tolist() == [30.0]

    def test_missing_column_is_reported_by_name(self, tmp_path: Path) -> None:
        frame = sd_frame().drop(columns=["Advertised ASIN"])
        with pytest.raises(ReportValidationError, match="Advertised ASIN"):
            transform_report(frame, SPONSORED_DISPLAY, source=tmp_path / "sd.xlsx")

    def test_empty_report_is_valid_even_without_headers(self, tmp_path: Path) -> None:
        result = transform_report(pd.DataFrame(), SPONSORED_DISPLAY, source=tmp_path / "x.csv")
        assert result.row_count == 0
        assert list(result.frame.columns) == list(OUTPUT_COLUMNS)

    def test_rows_without_a_date_are_dropped(self, tmp_path: Path) -> None:
        frame = pd.read_csv(pd.io.common.StringIO(sb_csv() + ",USD,Total,,,$1.00,1\n"), dtype=str)
        result = transform_report(frame, SPONSORED_BRANDS, source=tmp_path / "sb.csv")
        assert result.row_count == 2
        assert result.dropped_rows == 1

    def test_no_readable_date_at_all_is_an_error(self, tmp_path: Path) -> None:
        frame = sd_frame()
        frame["Date"] = "not a date"
        with pytest.raises(ReportValidationError, match="Daily"):
            transform_report(frame, SPONSORED_DISPLAY, source=tmp_path / "sd.xlsx")


class TestCleanNumber:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("$1,204.55", 1204.55), ("($45.10)", -45.10), ("12,480", 12480.0), ("0", 0.0)],
    )
    def test_cleans_currency_text(self, raw: str, expected: float) -> None:
        assert clean_number(pd.Series([raw])).iloc[0] == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "-", "N/A"])
    def test_blanks_become_nan(self, raw: str) -> None:
        assert pd.isna(clean_number(pd.Series([raw])).iloc[0])


class TestCombine:
    def test_concatenates_and_produces_plain_python_values(
        self, report_files: dict[str, Path], tmp_path: Path
    ) -> None:
        parts = [
            transform_report(sp_frame(), SPONSORED_PRODUCTS, source=tmp_path / "a"),
            transform_report(
                read_report_file(report_files["sb"]), SPONSORED_BRANDS, source=tmp_path / "b"
            ),
            transform_report(sd_frame(), SPONSORED_DISPLAY, source=tmp_path / "c"),
        ]
        combined = combine(parts)
        records = to_records(combined)

        assert len(records) == 5
        assert {type(r["Units"]) for r in records} == {int}
        assert {type(r["Sales"]) for r in records} == {float}
        assert records[0]["Attribution_type"] is None
