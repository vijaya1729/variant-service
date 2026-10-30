from collections.abc import Iterator
from pathlib import Path

import pixeltable_pgserver
import psycopg
import pytest

from app import db

MIGRATIONS = Path(__file__).parent.parent / "migrations"


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    # Real Postgres, because uniqueness and the results join live in the SQL.
    server = pixeltable_pgserver.get_server(tmp_path_factory.mktemp("pg"))
    url = server.get_uri()
    with psycopg.connect(url) as conn:
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(migration.read_text())
    yield url
    server.cleanup()


@pytest.fixture
def database(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    with db.connect() as conn:
        conn.execute("truncate experiments, variants, exposures, conversions")
