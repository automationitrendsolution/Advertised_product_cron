"""Read the three downloaded reports and shape them for the table.

This is the original upload script's processing, kept rule for rule:

* Sponsored Brands *Attributed Purchases* -> ``Purchased ASIN`` is the ASIN,
  14-day sales and units, and it is the only one with ``Attribution_type``.
* Sponsored Display *Advertised product* -> ``Advertised ASIN``, 14-day.
* Sponsored Products *Advertised product* -> ``Advertised ASIN``, 7-day.
* All three are concatenated, dates written as ``YYYY-MM-DD``.

What changed is robustness, not behaviour: any of the three may arrive as CSV
or XLSX (Amazon has switched formats before), currency text such as
``$1,204.55`` is cleaned for every report rather than only the CSV one, and a
report is recognised by its columns, so a file name is never relied on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .exceptions import ReportValidationError
from .logging_config import get_logger
from .schema import OUTPUT_COLUMNS, REPORTS, TEXT_COLUMNS, ReportSpec, normalise_header

logger = get_logger("processor")

#: Leading bytes of an XLSX (a zip archive).
_ZIP_MAGIC = b"PK\x03\x04"
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<?xml")
#: Cell text that means "no value".
_NULL_TEXT = {"", "-", "--", "n/a", "na", "nan", "none", "null"}


@dataclass
class ProcessedReport:
    """One report, mapped onto ``OUTPUT_COLUMNS``."""

    spec: ReportSpec
    source: Path
    frame: pd.DataFrame
    dropped_rows: int = 0

    @property
    def row_count(self) -> int:
        return len(self.frame)


def read_report_file(path: Path) -> pd.DataFrame:
    """Load a downloaded report, CSV or XLSX, everything as raw values.

    Raises:
        ReportValidationError: The file is missing, empty, an HTML page (a
            session that expired mid-download), or unparseable.
    """
    if not path.is_file():
        raise ReportValidationError(f"Report file does not exist: {path}")
    with path.open("rb") as handle:
        head = handle.read(512)
    if not head.strip():
        raise ReportValidationError(f"Report file {path} is empty")
    if head.lstrip().lower().startswith(_HTML_PREFIXES):
        raise ReportValidationError(
            f"Report file {path} is an HTML page, not a report. This usually means "
            "the Amazon session expired mid-download."
        )

    try:
        if head.startswith(_ZIP_MAGIC):
            frame = pd.read_excel(path, engine="openpyxl")
        else:
            frame = _read_csv(path)
    except ReportValidationError:
        raise
    except Exception as exc:
        raise ReportValidationError(f"Could not parse report {path}: {exc}") from exc

    # Fully blank rows are Excel padding, not data.
    frame = frame.dropna(how="all")
    logger.info("Read %s: %d row(s), %d column(s)", path.name, len(frame), len(frame.columns))
    return frame


def _read_csv(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False)
        except UnicodeDecodeError:
            continue
    raise ReportValidationError(f"Report {path} is not decodable text")  # pragma: no cover


def identify_report(frame: pd.DataFrame) -> ReportSpec:
    """Which of the three reports ``frame`` is, judged by its headers.

    Raises:
        ReportValidationError: If it matches none, or more than one.
    """
    headers = {normalise_header(column) for column in frame.columns}
    matches = [
        spec
        for spec in REPORTS
        if all(normalise_header(column) in headers for column in spec.columns)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ReportValidationError(
            "File is not one of the three advertised-product reports; its columns are: "
            + ", ".join(map(str, frame.columns))
        )
    raise ReportValidationError(
        "File matches more than one report type: " + ", ".join(s.label for s in matches)
    )


def transform_report(
    frame: pd.DataFrame,
    spec: ReportSpec,
    *,
    source: Path,
    date_format: str = "%Y-%m-%d",
) -> ProcessedReport:
    """Map one report onto ``OUTPUT_COLUMNS``.

    A report with no rows is valid -- early in the day there may be nothing to
    report -- and Amazon may omit the headers of an empty export, so the
    column check applies only when there are rows.

    Raises:
        ReportValidationError: A non-empty report is missing a required column,
            or none of its rows has a usable date.
    """
    if frame.empty:
        logger.info("%s has no rows", spec.label)
        return ProcessedReport(spec, source, pd.DataFrame(columns=list(OUTPUT_COLUMNS)))

    by_normalised = {normalise_header(column): column for column in frame.columns}
    missing = [h for h in spec.columns if normalise_header(h) not in by_normalised]
    if missing:
        raise ReportValidationError(
            f"{spec.label} report {source.name} is missing column(s): {', '.join(missing)}. "
            f"Columns present: {', '.join(map(str, frame.columns))}"
        )

    out = pd.DataFrame(index=frame.index)
    for amazon_header, target in spec.columns.items():
        out[target] = frame[by_normalised[normalise_header(amazon_header)]]
    for column in OUTPUT_COLUMNS:
        if column not in out.columns:
            out[column] = None
    out = out[list(OUTPUT_COLUMNS)]

    dates = pd.to_datetime(out["Date"], format="mixed", errors="coerce")
    undated = int(dates.isna().sum())
    if undated == len(out):
        raise ReportValidationError(
            f"{spec.label} report {source.name}: no row has a readable Date "
            f"(first value: {out['Date'].iloc[0]!r}). Was 'Daily' selected?"
        )
    if undated:
        # Amazon appends a totals row to some exports; it has no date.
        logger.warning(
            "%s: dropping %d row(s) without a readable Date", spec.label, undated
        )
    out = out[dates.notna()].copy()
    out["Date"] = dates[dates.notna()].dt.strftime(date_format)

    out["Sales"] = clean_number(out["Sales"]).astype(float)
    out["Units"] = clean_number(out["Units"]).round().astype("Int64")
    for column in TEXT_COLUMNS:
        out[column] = out[column].map(_clean_text)

    logger.info("%s: %d row(s) mapped", spec.label, len(out))
    return ProcessedReport(spec, source, out.reset_index(drop=True), dropped_rows=undated)


def clean_number(series: pd.Series) -> pd.Series:
    """``$1,204.55`` -> 1204.55, ``($45.10)`` -> -45.10, blanks -> NaN."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    text = series.astype("string").str.strip()
    text = text.mask(text.str.lower().isin(_NULL_TEXT))
    negative = text.str.match(r"^\(.*\)$", na=False)
    text = text.str.replace(r"[^\d.\-]", "", regex=True)
    numbers = pd.to_numeric(text, errors="coerce")
    return numbers.where(~negative, -numbers.abs()).astype(float)


def _clean_text(value: object) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip()
    return None if text.lower() in _NULL_TEXT else text


def combine(reports: list[ProcessedReport], *, date_column: str = "Date") -> pd.DataFrame:
    """Concatenate the processed reports into one frame for the table."""
    frames = [report.frame for report in reports if not report.frame.empty]
    if not frames:
        return pd.DataFrame(columns=[date_column, *OUTPUT_COLUMNS[1:]])
    combined = pd.concat(frames, ignore_index=True)
    return combined.rename(columns={"Date": date_column})


def to_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Frame -> insert rows of plain Python values, NaN/NA as ``None`` (NULL).

    mysql-connector rejects numpy scalars ("Python type numpy.int64 cannot be
    converted"), so every value is unwrapped here.
    """
    cleaned = frame.astype(object).where(frame.notna(), None)
    return [
        {key: value.item() if isinstance(value, np.generic) else value for key, value in row.items()}
        for row in cleaned.to_dict(orient="records")
    ]
