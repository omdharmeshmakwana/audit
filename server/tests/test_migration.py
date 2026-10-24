"""The integration migration (c4e7a1d2f9b3) is additive: a database at the previous head,
with a log in it, upgrades without touching a single event; the log still verifies and
rebuild still reproduces the derived tables (old data stays readable, DECISIONS #59)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from faible.log import verify_log
from faible.models import Event
from faible.rebuild import derived_checksum, rebuild
from faible.seed import seed
from tests.conftest import SERVER_DIR, TEST_DB_URL

PREVIOUS_HEAD = "b91c4d2e7f30"
DB = "faible_migration_test"


@pytest.fixture
def old_database() -> Iterator[tuple[Engine, Config]]:
    admin = create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {DB}"))
        conn.execute(text(f"CREATE DATABASE {DB}"))
    url = TEST_DB_URL.rsplit("/", 1)[0] + f"/{DB}"
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, PREVIOUS_HEAD)
    engine = create_engine(url)
    try:
        yield engine, cfg
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {DB}"))
        admin.dispose()


def test_upgrade_keeps_every_event_and_replays(
    old_database: tuple[Engine, Config], tmp_path: Path
) -> None:
    engine, cfg = old_database
    assert "service_generations" not in inspect(engine).get_table_names()
    with Session(engine) as session, session.begin():
        seed(session, tmp_path)
    with Session(engine) as session:
        before = [(e.seq, e.hash) for e in session.scalars(select(Event).order_by(Event.seq))]
    assert before

    command.upgrade(cfg, "head")

    assert {"service_generations", "usage_captures"} <= set(inspect(engine).get_table_names())
    with Session(engine) as session, session.begin():
        after = [(e.seq, e.hash) for e in session.scalars(select(Event).order_by(Event.seq))]
        assert after == before
        assert verify_log(session).ok
        checksum = derived_checksum(session)
        rebuild(session)
        assert derived_checksum(session).total == checksum.total
