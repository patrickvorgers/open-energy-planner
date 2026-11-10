"""SQLAlchemy storage adapter for matched forecasts and atomic daily assessments."""

from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.prices.policy import LEAD_BUCKET_COUNT
from open_energy_planner.prices.selection import ForecastScore
from open_energy_planner.prices.storage.adapters.queries.selection import load_states
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTUALS,
    COMPARISON_DECISIONS,
    COMPARISON_PAIRS,
    DECISIONS,
    PAIRS,
    PRICE_PAIR_TARGETS,
    SCHEMA,
    SELECTION_TABLES,
)
from open_energy_planner.storage.adapters.lifecycle.transactions import lifecycle_transaction

SQLITE_BUSY_TIMEOUT_SECONDS = 5


class SqlAlchemySelectionStore:
    """Own connections/SQL, keeping native database objects out of source-selection policy.

    A supplied SQLAlchemy Engine selects the backend. There is no silent local
    fallback on server failures. Writes share the app engine's complete writer guard.
    Schema creation is idempotent and uses only OEP-prefixed tables. Runtime updates use a single transaction across all tables.
    """

    def __init__(self, engine: Engine, *, sources=None):
        """Borrow a composition-owned engine. Defer schema work to guarded storage calls."""
        self.engine = engine
        self.pairs = COMPARISON_PAIRS if sources else PAIRS
        self.decisions = COMPARISON_DECISIONS if sources else DECISIONS
        self.identities = (
            dict(zip(("primary_source", "alternative_source"), sources)) if sources else {}
        )
        self.initialized = False
        self.path = (
            Path(engine.url.database)
            if engine.dialect.name == "sqlite" and engine.url.database
            else None
        )

    @classmethod
    def local(cls, directory):
        """Open isolated SQLite storage for adapter tests. App runtime borrows its active engine."""
        path = Path(directory) / "prices" / "selection.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(
            create_engine(
                URL.create("sqlite", database=str(path)),
                connect_args={"timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
            )
        )

    def _initialize(self):
        """Create missing tables and reconcile the scoring index without changing retained evidence.

        Existing tables are skipped by create_all. Replace an older index definition
        before serving queries. Failed DDL is reported through the public storage calls.
        Initialization uses the same guarded connection as its DDL and inspection.
        Ordinary reads remain independent once initialization has committed.
        """
        if not self.initialized:
            with lifecycle_transaction(self.engine) as connection:
                SCHEMA.create_all(connection, tables=SELECTION_TABLES)
                expected = [column.name for column in PRICE_PAIR_TARGETS.columns]
                for index in inspect(connection).get_indexes(PAIRS.name):
                    if (
                        index["name"] == PRICE_PAIR_TARGETS.name
                        and index["column_names"] != expected
                    ):
                        PRICE_PAIR_TARGETS.drop(connection)
                PRICE_PAIR_TARGETS.create(connection, checkfirst=True)
            self.initialized = True

    def _scope(self, table):
        """Keep every comparison isolated by its explicit provider identities."""
        return tuple(table.c[name] == value for name, value in self.identities.items())

    def load(self, area):
        """Restore persisted decisions or raise a sanitized fault without exposing connection credentials."""
        try:
            self._initialize()
            with self.engine.connect() as connection:
                return load_states(connection, area, self.decisions, self.identities)
        except SQLAlchemyError:
            raise OSError("forecast_selection_storage_unavailable") from None

    def update(self, area, pairs, actuals, day, window_start, decide):
        """Commit paired evidence, truth and daily policy decisions atomically on any supported dialect."""
        pair_table, decision_table = self.pairs, self.decisions
        try:
            self._initialize()
            with lifecycle_transaction(self.engine) as connection:
                for actual in actuals:
                    key = (ACTUALS.c.area == area) & (ACTUALS.c.target == actual.target.isoformat())
                    values = {"price": actual.price, "version": actual.version}
                    if connection.scalar(select(ACTUALS.c.target).where(key)) is not None:
                        connection.execute(ACTUALS.update().where(key).values(**values))
                    else:
                        connection.execute(
                            ACTUALS.insert().values(
                                area=area, target=actual.target.isoformat(), **values
                            )
                        )
                for pair in pairs:
                    known = (ACTUALS.c.area == area) & (ACTUALS.c.target == pair.target.isoformat())
                    key = (
                        (pair_table.c.area == area)
                        & (pair_table.c.bucket == pair.bucket)
                        & (pair_table.c.cohort == pair.cohort.isoformat())
                        & (pair_table.c.target == pair.target.isoformat())
                    )
                    if (
                        connection.scalar(select(ACTUALS.c.target).where(known)) is not None
                        or connection.scalar(
                            select(pair_table.c.target).where(key, *self._scope(pair_table))
                        )
                        is not None
                    ):
                        continue
                    connection.execute(
                        pair_table.insert().values(
                            **self.identities,
                            area=area,
                            bucket=pair.bucket,
                            cohort=pair.cohort.isoformat(),
                            target=pair.target.isoformat(),
                            primary_price=pair.primary_price,
                            alternative_price=pair.alternative_price,
                            primary_version=pair.primary_version,
                            alternative_version=pair.alternative_version,
                        )
                    )
                states = load_states(connection, area, self.decisions, self.identities)
                for bucket in range(LEAD_BUCKET_COUNT):
                    old = states.get(bucket, {})
                    if old.get("assessed_on") == day.date().isoformat():
                        continue
                    rows = connection.execute(
                        select(
                            pair_table.c.target,
                            func.avg(func.abs(pair_table.c.primary_price - ACTUALS.c.price)),
                            func.avg(func.abs(pair_table.c.alternative_price - ACTUALS.c.price)),
                        )
                        .join(
                            ACTUALS,
                            (pair_table.c.area == ACTUALS.c.area)
                            & (pair_table.c.target == ACTUALS.c.target),
                        )
                        .where(
                            *self._scope(pair_table),
                            pair_table.c.area == area,
                            pair_table.c.bucket == bucket,
                            pair_table.c.target >= window_start.isoformat(),
                            pair_table.c.target < day.isoformat(),
                        )
                        .group_by(pair_table.c.target)
                    ).all()
                    score = ForecastScore(
                        sum(row[1] for row in rows) / len(rows) if rows else None,
                        sum(row[2] for row in rows) / len(rows) if rows else None,
                        len(rows),
                        len({row[0][:10] for row in rows}),
                    )
                    decision = decide(old, score, day)
                    connection.execute(
                        decision_table.insert().values(
                            **self.identities,
                            area=area,
                            bucket=bucket,
                            day=decision["assessed_on"],
                            preferred=decision["preferred_source"],
                            challenger=decision["challenger"],
                            streak=decision["streak"],
                            primary_mae=decision["primary_mae"],
                            alternative_mae=decision["alternative_mae"],
                            hours=decision["matched_hours"],
                            days=decision["settled_days"],
                            reason=decision["reason"],
                        )
                    )
                return load_states(connection, area, self.decisions, self.identities)
        except SQLAlchemyError:
            raise OSError("forecast_selection_storage_unavailable") from None
