"""Shared assertions about the schema a test is running against.

Twenty-two assertions across sixteen test files used to spell the migration
ledger out as a literal -- ``list(range(1, 33))``, ``initialize()[-1] == 32``,
``initialize() == [34]``.  Every one of them was a fixture guard meaning "this
database came up on the current schema", and every one of them silently
stopped meaning that when migration 33 landed.  Fifty-seven tests about
Learning feedback, auto-post runtime, Dashboard startup, approval schema and
DPS re-entrancy then failed for a reason none of them was about.

So the expected ledger is read from ``MIGRATIONS`` once, here.  What is
checked against it is always the result of really running the migrations: the
list ``initialize()`` reports applying, the ``schema_migrations`` rows it
persisted, or the tables and columns it created.  Nothing below compares
``MIGRATIONS`` with itself.
"""

from __future__ import annotations

from typing import Any, Iterable

from repositories.database import MIGRATIONS, Database


def expected_migration_versions() -> list[int]:
    """Every migration version the current schema declares, in order."""

    return [int(version) for version, _ in MIGRATIONS]


def assert_fresh_schema(database: Database) -> Database:
    """A brand-new database comes up on the current schema.

    The fixture guard the literals were doing.  ``initialize()`` runs the
    migrations for real and returns what it applied, so this fails if any
    migration raises, is skipped, or runs twice -- it just no longer fails
    because a number moved.
    """

    assert database.initialize() == expected_migration_versions()
    return database


def assert_schema_ledger_is_complete(database: Database) -> None:
    """The full migration contract, for tests that are *about* migrating.

    Three separate claims, and none of them is the same claim twice:
    everything was applied in order, applying again is a no-op, and the
    ledger the first run persisted survives in the database.
    """

    assert database.initialize() == expected_migration_versions()
    assert database.initialize() == []
    assert database.migration_versions() == expected_migration_versions()


def assert_reinitialize_is_noop(database: Database) -> None:
    """An already-migrated database applies nothing on a second run.

    For the tests that have been working against a live database and then
    want the idempotence guarantee.  ``assert_schema_ledger_is_complete``
    cannot be used there: its first assertion is about a *fresh* database and
    would see an empty list.
    """

    assert database.initialize() == []
    assert database.migration_versions() == expected_migration_versions()


def assert_migration_applied(database: Database, version: int) -> None:
    """One named migration ran, whatever has been added after it."""

    assert int(version) in database.migration_versions()


def assert_tables_exist(database: Database, names: Iterable[str]) -> None:
    """The tables a migration was written to create are really there."""

    wanted = {str(name) for name in names}
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert wanted <= {str(row["name"]) for row in rows}


def assert_columns_exist(
    database: Database, table: str, names: Iterable[str]
) -> None:
    """The columns a migration was written to add are really there."""

    wanted = {str(name) for name in names}
    with database.connection() as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    assert wanted <= {str(row["name"]) for row in rows}


def migrations_through(version: int) -> tuple[Any, ...]:
    """``MIGRATIONS`` truncated just before ``version``.

    For the tests that build an older database on purpose and then upgrade it.
    Keyed on the version number rather than a slice index, because
    ``MIGRATIONS[:-1]`` meant "everything before 34" only until migration 35
    was written -- after which the test set up the wrong old schema and its
    assertion passed for the wrong reason.
    """

    target = int(version)
    cut = [index for index, (value, _) in enumerate(MIGRATIONS)
           if int(value) == target]
    assert cut, f"migration {target} is not declared"
    return MIGRATIONS[: cut[0]]
