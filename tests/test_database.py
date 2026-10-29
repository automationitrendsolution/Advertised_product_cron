"""The load: delete the dates in the data, insert the data, one transaction."""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from app.database import AdvertisedProductRepository
from app.exceptions import DatabaseError


def row(date: str = "2026-10-29", asin: str = "B1", sales: float = 1.0) -> dict[str, object]:
    return {
        "Date": date,
        "Currency": "USD",
        "Campaign_Name": "C",
        "Attribution_type": None,
        "ASIN": asin,
        "Sales": sales,
        "Units": 1,
    }


def seed(repository: AdvertisedProductRepository, rows: list[dict[str, object]]) -> None:
    with repository.engine.begin() as connection:
        connection.execute(insert(repository.table), rows)


class TestReplaceDates:
    def test_replaces_only_the_dates_present(self, repository: AdvertisedProductRepository) -> None:
        seed(repository, [row("2026-10-28", "OLD"), row("2026-10-29", "STALE")])

        result = repository.replace_dates([row("2026-10-29", "NEW1"), row("2026-10-29", "NEW2")])

        assert result.dates == ("2026-10-29",)
        assert (result.deleted, result.inserted) == (1, 2)
        assert repository.count_for_date("2026-10-28") == 1
        assert repository.count_for_date("2026-10-29") == 2

    def test_rerun_converges(self, repository: AdvertisedProductRepository) -> None:
        rows = [row(asin="A"), row(asin="B")]
        repository.replace_dates(rows)
        repository.replace_dates(rows)
        assert repository.count_for_date("2026-10-29") == 2

    def test_refuses_a_delete_with_nothing_to_insert(
        self, repository: AdvertisedProductRepository
    ) -> None:
        seed(repository, [row()])
        with pytest.raises(ValueError, match="zero records"):
            repository.replace_dates([])
        assert repository.count_for_date("2026-10-29") == 1

    def test_failed_insert_rolls_the_delete_back(
        self, repository: AdvertisedProductRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(repository, [row(asin="KEEP")])

        def explode(*_args, **_kwargs):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("INSERT", {}, Exception("boom"))

        monkeypatch.setattr("app.database._chunked_insert", explode)
        with pytest.raises(DatabaseError, match="rolled back"):
            repository.replace_dates([row(asin="NEW")])

        assert repository.count_for_date("2026-10-29") == 1

    def test_unknown_columns_are_dropped_and_absent_ones_null(
        self, repository: AdvertisedProductRepository
    ) -> None:
        record = row()
        record["Extra"] = "x"
        del record["Currency"]

        repository.replace_dates([record])

        with repository.engine.connect() as connection:
            stored = connection.execute(repository.table.select()).mappings().one()
        assert stored["Currency"] is None
        assert "Extra" not in stored
