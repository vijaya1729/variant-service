# Variant Service

A stateless, deterministic experiment assignment service with durable event tracking.

## Migrations

Each file runs once, in a single transaction, so a failed or repeated run changes nothing.

```sh
set -a; source .env; set +a
.venv/bin/python scripts/migrate.py migrations/001_init.sql
.venv/bin/python scripts/migrate.py migrations/002_generation_meta.sql
```

## Demo

`/demo` runs the `cta-test` experiment, which has to exist and be running. `/dashboard` shows its results.

```sh
BASE=http://127.0.0.1:8000
curl -X POST $BASE/experiments -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' -d '{
  "id": "cta-test", "traffic": 100,
  "variants": [
    {"key": "A", "weight": 50},
    {"key": "B", "weight": 50, "payload": {"headline": "Launch tests in minutes, not sprints"}}
  ]
}'
curl -X PATCH $BASE/experiments/cta-test/status -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{"status": "running"}'
```

## Traffic simulator

Sends visitors through assign, expose and convert, then prints the split and assign latency. It writes real events, so use a throwaway experiment.

```sh
.venv/bin/python scripts/simulate.py --base-url $BASE --experiment cta-test --rates A=0.05,B=0.07
```
