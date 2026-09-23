"""Test database: `faible_test`, migrated from scratch once per run. Each test runs inside a
transaction that is rolled back, so tests never see each other's rows (and the append-only
trigger never has to be bypassed to clean up)."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from alembic import command
from faible.config import get_settings
from faible.db import get_session
from faible.main import app
from faible.seed import seed

TEST_DB_URL = os.environ.get("FAIBLE_TEST_DATABASE_URL", "postgresql+psycopg:///faible_test")
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DB_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    conn = engine.connect()
    trans = conn.begin()
    s = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    seed(session, tmp_path)
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
