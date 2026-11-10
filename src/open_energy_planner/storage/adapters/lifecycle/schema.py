"""One portable schema for every OEP database table. Migration and runtime share this registry."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.mysql import LONGTEXT, VARCHAR

HASH_LENGTH = 64
IDENTITY_LENGTH = 128
FIELD_LENGTH = 128
TIMESTAMP_LENGTH = 40
AREA_LENGTH = 32
SOURCE_ID_LENGTH = 128
VERSION_LENGTH = 64
REASON_LENGTH = 64

SCHEMA = MetaData()


def archive_table(name, *columns):
    """Use portable tables with explicit InnoDB/utf8mb4 on MariaDB for real foreign-key transactions."""
    return Table(
        name,
        SCHEMA,
        *columns,
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mariadb_engine="InnoDB",
        mariadb_charset="utf8mb4",
    )


def text_key(length):
    """Keep identities and JSON field names case-sensitive on MariaDB as on SQLite/PostgreSQL."""
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_bin"), "mysql", "mariadb")


ACTIVITIES = archive_table(
    "oep_activities",
    Column("id", text_key(IDENTITY_LENGTH), primary_key=True),
    Column("started_at", String(TIMESTAMP_LENGTH), nullable=False),
    Column("owner", text_key(IDENTITY_LENGTH), nullable=False),
    Column("kind", String(FIELD_LENGTH), nullable=False),
    Column("state", String(FIELD_LENGTH), nullable=False),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
)
Index("oep_activity_chronology", ACTIVITIES.c.started_at, ACTIVITIES.c.id)


META = archive_table(
    "oep_archive_meta",
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
)
VALUES = archive_table(
    "oep_archive_values",
    Column("digest", text_key(HASH_LENGTH), primary_key=True),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
)
RECORDS = archive_table(
    "oep_archive_records", Column("digest", text_key(HASH_LENGTH), primary_key=True)
)
PARTS = archive_table(
    "oep_archive_parts",
    Column("owner", text_key(HASH_LENGTH), ForeignKey(RECORDS.c.digest), primary_key=True),
    Column("field", text_key(FIELD_LENGTH), primary_key=True),
    Column("value_ref", text_key(HASH_LENGTH), ForeignKey(VALUES.c.digest)),
    Column("record_ref", text_key(HASH_LENGTH), ForeignKey(RECORDS.c.digest)),
    CheckConstraint(
        "(value_ref IS NULL AND record_ref IS NOT NULL) OR (value_ref IS NOT NULL AND record_ref IS NULL)",
        name="oep_one_part_reference",
    ),
)
PLANS = archive_table(
    "oep_archive_plans",
    Column("plan_id", text_key(IDENTITY_LENGTH), primary_key=True),
    Column("record_ref", text_key(HASH_LENGTH), ForeignKey(RECORDS.c.digest), nullable=False),
    Column("generated_at", String(TIMESTAMP_LENGTH), nullable=False),
    Column("strategy", String(IDENTITY_LENGTH), nullable=False),
    Column("solution_status", String(IDENTITY_LENGTH), nullable=False),
)
Index("oep_plan_chronology", PLANS.c.generated_at, PLANS.c.plan_id)
ARCHIVE_INCOMING_INDEXES = (
    Index("oep_plan_record_reference", PLANS.c.record_ref),
    Index("oep_part_record_reference", PARTS.c.record_ref),
    Index("oep_part_value_reference", PARTS.c.value_ref),
)
STATE = archive_table(
    "oep_archive_state",
    Column("id", Integer, primary_key=True),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
    Column("plan_id", text_key(IDENTITY_LENGTH), ForeignKey(PLANS.c.plan_id)),
)
PLAN_CONTEXTS = archive_table(
    "oep_plan_contexts",
    Column("plan_id", text_key(IDENTITY_LENGTH), primary_key=True),
    Column("generated_at", String(TIMESTAMP_LENGTH), nullable=False),
    Column("controls", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
    Column("controls_hash", text_key(HASH_LENGTH), nullable=False),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb")),
    Column("payload_hash", text_key(HASH_LENGTH)),
)
Index("oep_plan_context_chronology", PLAN_CONTEXTS.c.generated_at, PLAN_CONTEXTS.c.plan_id)


def price_pair_columns():
    """Return independent columns for original and source-identified paired evidence."""
    return (
        Column("area", String(AREA_LENGTH), primary_key=True),
        Column("bucket", Integer, primary_key=True),
        Column("cohort", String(TIMESTAMP_LENGTH), primary_key=True),
        Column("target", String(TIMESTAMP_LENGTH), primary_key=True),
        Column("primary_price", Double, nullable=False),
        Column("alternative_price", Double, nullable=False),
        Column("primary_version", String(VERSION_LENGTH), nullable=False),
        Column("alternative_version", String(VERSION_LENGTH), nullable=False),
    )


PAIRS = archive_table("oep_price_pairs", *price_pair_columns())
PRICE_PAIR_TARGETS = Index("oep_price_pair_targets", PAIRS.c.area, PAIRS.c.bucket, PAIRS.c.target)
ACTUALS = archive_table(
    "oep_price_actuals",
    Column("area", String(AREA_LENGTH), primary_key=True),
    Column("target", String(TIMESTAMP_LENGTH), primary_key=True),
    Column("price", Double, nullable=False),
    Column("version", String(VERSION_LENGTH), nullable=False),
)


def price_decision_columns():
    """Retain the same daily assessment contract for each independently scoped source pair."""
    return (
        Column("area", String(AREA_LENGTH), primary_key=True),
        Column("bucket", Integer, primary_key=True),
        Column("day", String(TIMESTAMP_LENGTH), primary_key=True),
        Column("preferred", String(SOURCE_ID_LENGTH), nullable=False),
        Column("challenger", String(SOURCE_ID_LENGTH)),
        Column("streak", Integer, nullable=False),
        Column("primary_mae", Double),
        Column("alternative_mae", Double),
        Column("hours", Integer, nullable=False),
        Column("days", Integer, nullable=False),
        Column("reason", String(REASON_LENGTH), nullable=False),
    )


DECISIONS = archive_table("oep_price_decisions", *price_decision_columns())

COMPARISON_PAIRS = archive_table(
    "oep_price_comparison_pairs",
    *price_pair_columns(),
    Column("primary_source", String(SOURCE_ID_LENGTH), primary_key=True),
    Column("alternative_source", String(SOURCE_ID_LENGTH), primary_key=True),
)
Index(
    "oep_comparison_targets",
    COMPARISON_PAIRS.c.area,
    COMPARISON_PAIRS.c.primary_source,
    COMPARISON_PAIRS.c.alternative_source,
    COMPARISON_PAIRS.c.bucket,
    COMPARISON_PAIRS.c.target,
)
COMPARISON_DECISIONS = archive_table(
    "oep_price_comparison_decisions",
    *price_decision_columns(),
    Column("primary_source", String(SOURCE_ID_LENGTH), primary_key=True),
    Column("alternative_source", String(SOURCE_ID_LENGTH), primary_key=True),
)
Index(
    "oep_comparison_latest",
    COMPARISON_DECISIONS.c.area,
    COMPARISON_DECISIONS.c.primary_source,
    COMPARISON_DECISIONS.c.alternative_source,
    COMPARISON_DECISIONS.c.bucket,
    COMPARISON_DECISIONS.c.day,
)
SELECTION_TABLES = (PAIRS, ACTUALS, DECISIONS, COMPARISON_PAIRS, COMPARISON_DECISIONS)

DOCUMENTS = archive_table(
    "oep_documents",
    Column("namespace", text_key(IDENTITY_LENGTH), primary_key=True),
    Column("name", text_key(IDENTITY_LENGTH), primary_key=True),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
)

ACTIVATIONS = archive_table(
    "oep_plan_activations",
    Column("activated_at", String(TIMESTAMP_LENGTH), primary_key=True),
    Column("plan_id", text_key(IDENTITY_LENGTH), ForeignKey(PLANS.c.plan_id)),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("context_hash", text_key(HASH_LENGTH), nullable=False, server_default=""),
)
ACTIVATION_CONTEXTS = archive_table(
    "oep_activation_contexts",
    Column(
        "activated_at",
        String(TIMESTAMP_LENGTH),
        ForeignKey(ACTIVATIONS.c.activated_at),
        primary_key=True,
    ),
    Column(
        "plan_id", text_key(IDENTITY_LENGTH), ForeignKey(PLAN_CONTEXTS.c.plan_id), nullable=False
    ),
)
SETTLEMENT_FRAMES = archive_table(
    "oep_settlement_frames",
    Column("start", String(TIMESTAMP_LENGTH), primary_key=True),
    Column("end", String(TIMESTAMP_LENGTH), nullable=False),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
)
SETTLEMENT_EVALUATIONS = archive_table(
    "oep_settlement_evaluations",
    Column("revision", text_key(HASH_LENGTH), primary_key=True),
    Column(
        "start", String(TIMESTAMP_LENGTH), ForeignKey(SETTLEMENT_FRAMES.c.start), primary_key=True
    ),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
    *(
        Column(f"{scenario}_{key}", Double, nullable=False)
        for scenario in ("actual", "reference")
        for key in (
            "purchase",
            "sale",
            "bonus",
            "grid_cash",
            "wear",
            "inventory_change",
            "economic_cost",
            "import_kwh",
            "export_kwh",
        )
    ),
    Column("savings", Double, nullable=False),
    Column("avoided_import_kwh", Double, nullable=False),
    Column("estimated", Integer, nullable=False),
    Column("duration_h", Double, nullable=False),
)
SETTLEMENT_STATE = archive_table(
    "oep_settlement_state",
    Column("revision", text_key(HASH_LENGTH), primary_key=True),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
)
SAVINGS_DAYS = archive_table(
    "oep_savings_days",
    Column("start", String(TIMESTAMP_LENGTH), primary_key=True),
    Column("end", String(TIMESTAMP_LENGTH), nullable=False),
    Column("day", String(TIMESTAMP_LENGTH), nullable=False),
    Column("timezone", String(IDENTITY_LENGTH), nullable=False),
    Column("currency", String(AREA_LENGTH), nullable=False),
    Column("market_area", String(AREA_LENGTH), nullable=False),
    Column("finalized_at", String(TIMESTAMP_LENGTH)),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql", "mariadb"), nullable=False),
    Column("payload_hash", text_key(HASH_LENGTH), nullable=False),
    *(
        Column(f"{scenario}_{key}", Double)
        for scenario in ("actual", "reference", "without_battery")
        for key in (
            "purchase",
            "sale",
            "bonus",
            "grid_cash",
            "wear",
            "inventory_change",
            "economic_cost",
            "import_kwh",
            "export_kwh",
            "coverage_seconds",
        )
    ),
    Column("savings", Double),
    Column("avoided_import_kwh", Double),
    Column("comparison_seconds", Double, nullable=False),
    Column("battery_savings", Double),
    Column("battery_comparison_seconds", Double, nullable=False),
    Column("self_consumption_savings", Double),
    Column("self_consumption_comparison_seconds", Double, nullable=False),
)
Index("oep_savings_day_finalization", SAVINGS_DAYS.c.finalized_at, SAVINGS_DAYS.c.start)
PUBLISHED_PRICES = archive_table(
    "oep_published_prices",
    Column("area", String(AREA_LENGTH), primary_key=True),
    Column("currency", String(AREA_LENGTH), primary_key=True),
    Column("start", String(TIMESTAMP_LENGTH), primary_key=True),
    Column("end", String(TIMESTAMP_LENGTH), nullable=False),
    Column("received_at", String(TIMESTAMP_LENGTH), nullable=False),
    Column("version", text_key(HASH_LENGTH), nullable=False),
    Column("price", Double, nullable=False),
)

PUBLISHED_EVENTS = archive_table(
    "oep_published_price_events",
    Column("sequence", Integer, primary_key=True, autoincrement=False),
    Column("area", String(AREA_LENGTH), nullable=False),
    Column("currency", String(AREA_LENGTH), nullable=False),
    Column("start", String(TIMESTAMP_LENGTH), nullable=False),
    Column("version", text_key(HASH_LENGTH), nullable=False),
)
Index(
    "oep_published_event_area",
    PUBLISHED_EVENTS.c.area,
    PUBLISHED_EVENTS.c.currency,
    PUBLISHED_EVENTS.c.sequence,
)
