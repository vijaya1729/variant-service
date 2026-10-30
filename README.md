# Variant Service

A stateless, deterministic experiment assignment service with durable event tracking.

## Migrations

Each file runs once, in a single transaction, so a failed or repeated run changes nothing.

```sh
set -a; source .env; set +a
.venv/bin/python scripts/migrate.py migrations/001_init.sql
```
