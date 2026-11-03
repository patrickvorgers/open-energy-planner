"""Read persisted test state through the real storage adapter without leaking database engines."""

from contextlib import closing

from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import SqlPlanRepository

STORE_NAMESPACES = {
    "ContractStore": "contracts",
    "CounterStore": "bonus-counters",
    "DemandStore": "demand",
    "SolarStore": "solar",
    "InstallationStore": "installation",
    "PriceStore": "prices",
}


def read_plan(directory, plan_id):
    """Reopen the archive and detach one exact historical document for cross-request assertions."""
    with closing(SqlPlanRepository(directory)) as archive:
        return archive.get_plan(plan_id)


def read_state(directory):
    """Read committed state rather than relying on the app's mutable in-memory repository."""
    with closing(SqlPlanRepository(directory)) as archive:
        return archive.state


def list_plans(directory):
    """Read committed plan identities for publication/no-publication assertions."""
    with closing(SqlPlanRepository(directory)) as archive:
        return archive.list_plans(limit=100)


def read_current(directory, store_type):
    """Reload typed active configuration through the shared database and close its borrowed resources."""
    from open_energy_planner.storage.adapters.documents import SqlDocuments

    with closing(SqlPlanRepository(directory)) as archive:
        return store_type(
            SqlDocuments(archive.engine, STORE_NAMESPACES[store_type.__name__])
        ).current


def read_version(directory, store_type, version):
    """Read one typed original contract/counter version without returning a live database handle."""
    from open_energy_planner.storage.adapters.documents import SqlDocuments

    with closing(SqlPlanRepository(directory)) as archive:
        return store_type(SqlDocuments(archive.engine, STORE_NAMESPACES[store_type.__name__])).load(
            version
        )


def list_documents(directory, namespace):
    """List committed document identities for no-side-effect and atomic-publication assertions."""
    from sqlalchemy import select

    from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENTS

    with (
        closing(SqlPlanRepository(directory)) as archive,
        archive.engine.connect() as connection,
    ):
        return list(
            connection.scalars(select(DOCUMENTS.c.name).where(DOCUMENTS.c.namespace == namespace))
        )


def corrupt_document(directory, namespace, name, raw_payload):
    """Inject malformed database evidence without changing pointers or bypassing the production reader."""
    from sqlalchemy import select

    from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENTS

    with (
        closing(SqlPlanRepository(directory)) as archive,
        archive.engine.begin() as connection,
    ):
        key = (DOCUMENTS.c.namespace == namespace) & (DOCUMENTS.c.name == name)
        connection.execute(DOCUMENTS.update().where(key).values(payload=raw_payload))
        assert connection.scalar(select(DOCUMENTS.c.payload).where(key)) == raw_payload


def read_document(directory, namespace, name):
    """Read exact persisted text so immutable evidence tests do not depend on filesystem layout."""
    from sqlalchemy import select

    from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENTS

    with (
        closing(SqlPlanRepository(directory)) as archive,
        archive.engine.connect() as connection,
    ):
        return connection.scalar(
            select(DOCUMENTS.c.payload).where(
                DOCUMENTS.c.namespace == namespace, DOCUMENTS.c.name == name
            )
        )


def make_store(directory, store_type):
    """Create a typed test store on the shared SQLite archive, disposing its owner when the store is released."""
    import weakref

    from open_energy_planner.storage.adapters.documents import SqlDocuments

    owner = SqlPlanRepository(directory)
    store = store_type(SqlDocuments(owner.engine, STORE_NAMESPACES[store_type.__name__]))
    weakref.finalize(store, owner.close)
    return store
