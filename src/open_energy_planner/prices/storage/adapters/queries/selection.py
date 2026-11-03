"""Read indexed decision projections without modifying schema or retained rows."""

from sqlalchemy import select

from open_energy_planner.prices.policy import LEAD_BUCKET_COUNT


def load_states(connection, area, decision_table, identities):
    """Read at most one indexed latest decision per lead bucket, independent of retained years.

    The caller owns the transaction. This helper changes neither schema nor rows.
    SQLAlchemy faults propagate to the public store methods for sanitized translation.

    Args:
        connection: Borrowed SQLAlchemy connection.
        area: Market bidding area used as an equality filter.
        decision_table: Decision table to query.
        identities: Decision-table column names and required identity values.

    Returns:
        Mapping from integer lead bucket to its latest persisted decision. Buckets
        without a decision are absent. Each decision includes the stored day string
        as assessed_on, nullable primary_mae and alternative_mae in EUR/kWh, and
        the remaining selection state.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: A connection operation failed.
    """
    rows = []
    for bucket in range(LEAD_BUCKET_COUNT):
        row = (
            connection.execute(
                select(decision_table)
                .where(
                    decision_table.c.area == area,
                    decision_table.c.bucket == bucket,
                    *(decision_table.c[name] == value for name, value in identities.items()),
                )
                .order_by(decision_table.c.day.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is not None:
            rows.append(row)
    return {
        row["bucket"]: {
            "assessed_on": row["day"],
            "preferred_source": row["preferred"],
            "challenger": row["challenger"],
            "streak": row["streak"],
            "primary_mae": row["primary_mae"],
            "alternative_mae": row["alternative_mae"],
            "matched_hours": row["hours"],
            "settled_days": row["days"],
            "reason": row["reason"],
        }
        for row in rows
    }
