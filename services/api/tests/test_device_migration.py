"""Exercise the shipped DDL on the harness's disposable Postgres database."""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect


async def test_device_grant_migration_roundtrip(engine):
    migration = importlib.import_module("potluck_api.db.migrations.versions.0011_device_grants")
    def roundtrip(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()  # Harness started with the metadata schema.
            assert "device_grants" not in inspect(connection).get_table_names()
            migration.upgrade()
            columns = {column["name"]: column for column in inspect(connection).get_columns("device_grants")}
            assert set(columns) == {"id", "device_code_hash", "user_code_hash", "status", "member_id", "expires_at", "last_polled_at", "interval"}
            assert not columns["device_code_hash"]["nullable"]
            unique = inspect(connection).get_unique_constraints("device_grants")
            assert {tuple(item["column_names"]) for item in unique} == {("device_code_hash",), ("user_code_hash",)}
            assert inspect(connection).get_foreign_keys("device_grants")[0]["referred_table"] == "members"
    async with engine.begin() as connection:
        await connection.run_sync(roundtrip)
