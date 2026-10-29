"""The three reports AdProduct builds, and the table they are loaded into.

Each :class:`ReportSpec` is one "Create report" pass through the Sponsored ads
reports page (category -> report type -> Daily -> period -> Run report), plus
how its columns map onto the ``Advertised_product`` table.  The mappings are
the ones the original upload script used.

Amazon's headers are matched after whitespace normalisation and case folding:
the exports really do carry ``'14 Day Total Sales '`` with a trailing space,
and a mapping keyed on the exact spelling breaks the day Amazon trims it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text


class Period(str, Enum):
    """Which "Report period" preset a run selects."""

    TODAY = "today"
    YESTERDAY = "yesterday"

    @property
    def preset(self) -> str:
        """The preset's label in Amazon's date picker."""
        return self.value.capitalize()

    @property
    def offset_days(self) -> int:
        return 0 if self is Period.TODAY else -1


@dataclass(frozen=True)
class ReportSpec:
    """One Amazon report and how it lands in the table."""

    #: Short stable key, used in file names and CLI output.
    key: str
    #: "Report category" dropdown value.
    category: str
    #: "Report type" dropdown value.
    report_type: str
    #: Amazon header -> table column.  Every entry is required.
    columns: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.category} {self.report_type}"


SPONSORED_PRODUCTS = ReportSpec(
    key="sp",
    category="Sponsored Products",
    report_type="Advertised product",
    columns={
        "Date": "Date",
        "Currency": "Currency",
        "Campaign Name": "Campaign_Name",
        "Advertised ASIN": "ASIN",
        "7 Day Total Sales": "Sales",
        "7 Day Total Units (#)": "Units",
    },
)

SPONSORED_BRANDS = ReportSpec(
    key="sb",
    category="Sponsored Brands",
    report_type="Attributed Purchases",
    columns={
        "Date": "Date",
        "Currency": "Currency",
        "Campaign Name": "Campaign_Name",
        "Attribution type": "Attribution_type",
        "Purchased ASIN": "ASIN",
        "14 Day Total Sales": "Sales",
        "14 Day Total Units (#)": "Units",
    },
)

SPONSORED_DISPLAY = ReportSpec(
    key="sd",
    category="Sponsored Display",
    report_type="Advertised product",
    columns={
        "Date": "Date",
        "Currency": "Currency",
        "Campaign Name": "Campaign_Name",
        "Advertised ASIN": "ASIN",
        "14 Day Total Sales": "Sales",
        "14 Day Total Units (#)": "Units",
    },
)

#: In the order they are created on Amazon -- the order of the screenshots.
REPORTS: tuple[ReportSpec, ...] = (SPONSORED_PRODUCTS, SPONSORED_BRANDS, SPONSORED_DISPLAY)
REPORTS_BY_KEY = {spec.key: spec for spec in REPORTS}

#: Columns of the combined frame, in table order.
OUTPUT_COLUMNS: tuple[str, ...] = (
    "Date",
    "Currency",
    "Campaign_Name",
    "Attribution_type",
    "ASIN",
    "Sales",
    "Units",
)
TEXT_COLUMNS = ("Currency", "Campaign_Name", "Attribution_type", "ASIN")

_SPACES = re.compile(r"\s+")


def normalise_header(header: object) -> str:
    """``' 14 Day  Total Sales '`` -> ``'14 day total sales'``."""
    return _SPACES.sub(" ", str(header)).strip().casefold()


def build_bootstrap_table(name: str, date_column: str = "Date") -> tuple[MetaData, Table]:
    """Table definition used only by ``CREATE_TABLE=true`` on an empty database.

    The live table is always reflected and is authoritative; this is the
    shape the original script's ``to_sql`` would have produced.
    """
    metadata = MetaData()
    table = Table(
        name,
        metadata,
        Column(date_column, String(10)),
        Column("Currency", String(8)),
        Column("Campaign_Name", Text),
        Column("Attribution_type", String(64)),
        Column("ASIN", String(20)),
        Column("Sales", Float),
        Column("Units", Integer),
    )
    return metadata, table
