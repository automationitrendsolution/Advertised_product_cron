"""MySQL persistence for the ``Advertised_product`` table.

The table already exists in production, so AdProduct **reflects** it and
writes the columns that are really there.  It never issues DDL against an
existing table; ``CREATE_TABLE=true`` only creates one that is entirely absent.

The load is the original script's -- delete every date present in the new
data, then insert the data -- with one difference that matters: both happen in
a single transaction.  The table is never observed with a day deleted but not
replaced, and a failed insert rolls the delete back with it.  A delete is only
ever issued alongside rows to insert.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import (
    Integer,
    MetaData,
    Table,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import DatabaseConfig, Settings
from .exceptions import DatabaseError
from .logging_config import get_logger
from .schema import build_bootstrap_table

logger = get_logger("database")

#: Rows per INSERT batch: fast, while staying well under a default MySQL
#: ``max_allowed_packet``.
INSERT_CHUNK_SIZE = 500

#: MySQL errors a second attempt can plausibly get past: the connection could
#: not be made or dropped mid-statement, or the transaction lost a lock race.
TRANSIENT_MYSQL_ERRNOS = frozenset(
    {
        1205,  # lock wait timeout exceeded
        1213,  # deadlock found
        2003,  # can't connect to server
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection (I/O error)
    }
)


def _database_error(message: str, exc: SQLAlchemyError) -> DatabaseError:
    """Wrap a driver failure, marking it transient when a retry could succeed."""
    error = DatabaseError(message)
    errno = getattr(getattr(exc, "orig", None), "errno", None)
    error.transient = bool(
        getattr(exc, "connection_invalidated", False) or errno in TRANSIENT_MYSQL_ERRNOS
    )
    return error


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one load."""

    table: str
    dates: tuple[str, ...]
    deleted: int
    inserted: int


def create_db_engine(config: DatabaseConfig, *, echo: bool = False) -> Engine:
    """Build a pooled engine.  The URL is never logged with its password."""
    try:
        engine = create_engine(
            config.url,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=config.connect_args(),
        )
    except SQLAlchemyError as exc:
        raise DatabaseError(
            f"Could not create database engine for {config.safe_url}: {exc}"
        ) from exc
    logger.debug("Engine created for %s", config.safe_url)
    return engine


class AdvertisedProductRepository:
    """All database access for the advertised-product table."""

    def __init__(self, engine: Engine, *, table: str, date_column: str = "Date") -> None:
        self.engine = engine
        self.table_name = table
        self.date_column = date_column
        self._metadata = MetaData()
        self._table: Table | None = None

    # -- connectivity and schema --------------------------------------------

    def check_connection(self) -> None:
        """Fail fast if MySQL is unreachable."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {exc}", exc) from exc
        logger.info("Database connection verified")

    def create_table_if_missing(self) -> bool:
        """Create the table when it does not exist.  Never alters an existing one."""
        try:
            if self.table_name in inspect(self.engine).get_table_names():
                return False
            metadata, table = build_bootstrap_table(self.table_name, self.date_column)
            metadata.create_all(self.engine, tables=[table])
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not create table {self.table_name!r}: {exc}") from exc
        logger.info("Created missing table %s", self.table_name)
        return True

    def reflect(self) -> None:
        """Load the live table definition.

        Raises:
            DatabaseError: If the table is missing or has no date column.
        """
        try:
            self._table = Table(self.table_name, self._metadata, autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Could not reflect table {self.table_name!r}: {exc}. Create the table "
                "first, or set CREATE_TABLE=true.",
                exc,
            ) from exc
        if self.date_column not in self._table.columns:
            raise DatabaseError(
                f"Table {self.table_name!r} has no column {self.date_column!r}. "
                "Set DB_DATE_COLUMN to the column that holds the report date."
            )
        logger.info("Reflected %s with %d column(s)", self.table_name, len(self._table.columns))

    @property
    def table(self) -> Table:
        if self._table is None:
            self.reflect()
        assert self._table is not None
        return self._table

    def writable_columns(self) -> tuple[str, ...]:
        """Live columns minus auto-generated keys, in table order."""
        return tuple(
            column.name
            for column in self.table.columns
            if not (
                column.primary_key
                and column.autoincrement in (True, "auto")
                and isinstance(column.type, Integer)
            )
        )

    def align(self, records: Sequence[dict[str, object]]) -> list[dict[str, object]]:
        """Shape rows to the live table: drop unknown keys, NULL absent columns.

        Drift is logged once per run so it is visible rather than silent.
        """
        writable = self.writable_columns()
        if records:
            supplied = set(records[0])
            extra = sorted(supplied - set(writable))
            absent = sorted(set(writable) - supplied)
            if extra:
                logger.warning(
                    "Column(s) not in %s, not written: %s", self.table_name, ", ".join(extra)
                )
            if absent:
                logger.warning(
                    "Table column(s) with no data, written as NULL: %s", ", ".join(absent)
                )
        return [{name: row.get(name) for name in writable} for row in records]

    # -- transactions --------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN / COMMIT, with ROLLBACK on any exception."""
        connection = self.engine.connect()
        try:
            transaction = connection.begin()
            try:
                yield connection
            except Exception as exc:
                try:
                    transaction.rollback()
                except SQLAlchemyError as rollback_exc:
                    # A dead connection cannot roll back, but the server drops
                    # the uncommitted work itself. Raise the error that caused it.
                    logger.warning("ROLLBACK failed: %s", rollback_exc)
                logger.error("ROLLBACK after error: %s: %s", type(exc).__name__, exc)
                raise
            transaction.commit()
        finally:
            connection.close()

    # -- writes --------------------------------------------------------------

    def replace_dates(self, records: Sequence[dict[str, object]]) -> WriteResult:
        """Replace every date present in ``records`` with ``records``, atomically.

        Raises:
            ValueError: If ``records`` is empty -- a delete with nothing to
                insert is never a valid load.
            DatabaseError: On any database failure; the transaction is rolled
                back first, so the existing data survives intact.
        """
        if not records:
            raise ValueError("replace_dates called with zero records; refusing to delete data")

        rows = self.align(records)
        dates = tuple(sorted({str(row[self.date_column]) for row in rows}))
        table = self.table
        try:
            with self.transaction() as connection:
                deleted = connection.execute(
                    delete(table).where(table.c[self.date_column].in_(dates))
                ).rowcount
                logger.info("Deleted %s existing row(s) for %s", deleted, ", ".join(dates))
                inserted = _chunked_insert(connection, table, rows)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Load for {', '.join(dates)} failed and was rolled back: {exc}", exc
            ) from exc

        logger.info(
            "Committed: %s deleted, %s inserted into %s", deleted, inserted, self.table_name
        )
        return WriteResult(self.table_name, dates, max(deleted or 0, 0), inserted)

    def count_for_date(self, date_value: str) -> int:
        """Row count for one date -- used by verification and tests."""
        table = self.table
        try:
            with self.engine.connect() as connection:
                return int(
                    connection.execute(
                        select(func.count())
                        .select_from(table)
                        .where(table.c[self.date_column] == date_value)
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not count rows in {self.table_name}: {exc}") from exc

    def dispose(self) -> None:
        """Release pooled connections."""
        self.engine.dispose()


def _chunked_insert(
    connection: Connection, table: Table, records: Sequence[dict[str, object]]
) -> int:
    """Insert records in batches; returns the number of rows sent."""
    total = 0
    for start in range(0, len(records), INSERT_CHUNK_SIZE):
        chunk = list(records[start : start + INSERT_CHUNK_SIZE])
        connection.execute(insert(table), chunk)
        total += len(chunk)
    return total


def build_repository(settings: Settings, *, echo: bool = False) -> AdvertisedProductRepository:
    """Create a repository, verify connectivity, and reflect the live table."""
    repository = AdvertisedProductRepository(
        create_db_engine(settings.database, echo=echo),
        table=settings.table,
        date_column=settings.date_column,
    )
    try:
        repository.check_connection()
        if settings.create_table:
            repository.create_table_if_missing()
        repository.reflect()
    except Exception:
        repository.dispose()
        raise
    return repository
