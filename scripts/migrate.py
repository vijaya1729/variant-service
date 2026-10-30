"""Apply one SQL migration to DATABASE_URL: python scripts/migrate.py migrations/001_init.sql"""

import os
import sys
from pathlib import Path

import psycopg


def main(path: str) -> None:
    sql = Path(path).read_text()
    with psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None, connect_timeout=3) as conn:
        conn.execute(sql)
    print(f"applied {path}")


if __name__ == "__main__":
    main(sys.argv[1])
