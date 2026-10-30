# Variant Service

A stateless, deterministic experiment assignment service with durable event tracking. A page asks which variant a visitor should see, gets an answer without touching the database, and keeps its default content if anything goes wrong.

- **Live:** https://variant-service.vercel.app
- **Demo:** https://variant-service.vercel.app/demo
- **Dashboard:** https://variant-service.vercel.app/dashboard
- **Credentials:** admin key shared separately

## Overview

The service has three paths with very different requirements:

| Path | What it does | How often | If it fails |
|---|---|---|---|
| **Configure** | Create experiments, generate copy with an LLM, approve it, start and stop | Rare | The admin retries |
| **Run** | Assign a variant, then track exposures and conversions | Every page view | The page keeps its default content |
| **Measure** | Per-variant rates, confidence intervals, significance and SRM checks | Occasional | The admin retries |

Assignment is a pure function of the visitor ID and the experiment config, so `/assign` reads an in-memory cache and never queries the database directly. The LLM only runs at configure time, never on the page-render path.

## API

Admin endpoints need the `X-Admin-Key` header. Public endpoints allow any origin and accept JSON or `text/plain` bodies, which lets the snippet avoid a CORS preflight.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/experiments` | Admin | Create a draft experiment |
| GET | `/experiments/{id}` | Admin | Read an experiment |
| PATCH | `/experiments/{id}/status` | Admin | Change status: draft → running ↔ paused → stopped |
| POST | `/experiments/{id}/variants/{key}/generate` | Admin | Generate variant copy while in draft |
| POST | `/experiments/{id}/variants/{key}/approve` | Admin | Approve a variant's copy |
| GET | `/results/{id}?goal=default` | Admin | Per-variant results and SRM check |
| POST | `/assign` | Public | Get variants for a visitor |
| POST | `/expose` | Public | Record an exposure, always 202 |
| POST | `/convert` | Public | Record a conversion, always 202 |
| GET | `/snippet.js` | Public | Browser client |
| GET | `/demo`, `/dashboard` | Public | Demo page and results page (results need the key) |

### Walkthrough

```sh
BASE=https://variant-service.vercel.app
ADMIN_KEY=...  # shared separately
```

Create a draft:

```sh
curl -X POST $BASE/experiments \
  -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{
    "id": "pricing-headline",
    "traffic": 100,
    "variants": [
      {"key": "A", "weight": 50, "payload": {"headline": "Simple pricing"}},
      {"key": "B", "weight": 50, "payload": {"headline": "Simple pricing"}}
    ]
  }'
```

Generate copy for a variant. If this fails it returns 502 and leaves the existing copy in place:

```sh
curl -X POST $BASE/experiments/pricing-headline/variants/B/generate \
  -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"field": "headline", "brief": "Pricing page for small teams, goal is trial signups", "tone": "plain"}'
```

Approve it. An experiment can't start while any variant is unapproved:

```sh
curl -X POST $BASE/experiments/pricing-headline/variants/B/approve \
  -H "X-Admin-Key: $ADMIN_KEY"
```

Start the experiment. Each instance picks this up within the config TTL:

```sh
curl -X PATCH $BASE/experiments/pricing-headline/status \
  -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"status": "running"}'
```

Assign a visitor. `null` means show the default content:

```sh
curl -i -X POST $BASE/assign \
  -H 'Content-Type: text/plain' \
  -d '{"visitor_id": "visitor-1", "experiment_ids": ["pricing-headline"]}'
```

Record an exposure and a conversion. The server recomputes the variant, so the client never sends one:

```sh
curl -X POST $BASE/expose \
  -H 'Content-Type: text/plain' \
  -d '{"experiment_id": "pricing-headline", "visitor_id": "visitor-1"}'

curl -X POST $BASE/convert \
  -H 'Content-Type: text/plain' \
  -d '{"experiment_id": "pricing-headline", "visitor_id": "visitor-1", "goal": "default"}'
```

Read the results:

```sh
curl $BASE/results/pricing-headline -H "X-Admin-Key: $ADMIN_KEY"
```

### Browser snippet

```html
<script src="https://variant-service.vercel.app/snippet.js"></script>
<script>
  variants.run({
    endpoint: "https://variant-service.vercel.app",
    experiments: ["pricing-headline"],
    onAssign(assignments) {
      const assignment = assignments["pricing-headline"];
      if (assignment) {
        document.getElementById("headline").textContent = assignment.payload.headline;
      }
    },
  });
</script>
```

`onAssign` gets nulls after a 200ms timeout or any error. An exposure is sent after `onAssign` returns, unless it throws or returns `false`. Call `variants.convert("pricing-headline", "default")` on conversion.

## Environment variables

| Name | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | Postgres connection string. On Supabase, use the transaction pooler on port 6543 |
| `ADMIN_API_KEY` | Yes | Key for admin endpoints, which all return 401 while it's unset |
| `CONFIG_TTL_SECONDS` | No | How long an instance serves cached config. Default 30 |
| `LLM_API_KEY` | For generation | Anthropic API key, used server-side only |
| `LLM_MODEL` | No | Model for generation. Default `claude-haiku-5-5` |

## Running locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # then fill it in
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --reload
```

The app is then at http://127.0.0.1:8000, with the demo at `/demo`. The demo needs a running experiment called `cta-test`, which you can create with the walkthrough above.

## Migrations

Each file runs once, in a single transaction, so a failed or repeated run changes nothing.

```sh
set -a; source .env; set +a
.venv/bin/python scripts/migrate.py migrations/001_init.sql
.venv/bin/python scripts/migrate.py migrations/002_generation_meta.sql
```

## Tests

```sh
.venv/bin/pytest -s
```

Tests start their own embedded Postgres, so no Docker or database URL is needed, and they never call the real LLM API. `-s` prints the observed traffic splits.

## Traffic simulator

Sends visitors through assign, expose and convert, then prints the split and the round-trip and server-side assign latency. It writes real events, so point it at a throwaway experiment unless you want them in the results.

```sh
.venv/bin/python scripts/simulate.py --base-url https://variant-service.vercel.app \
  --experiment cta-test --rates A=0.05,B=0.07
```

`--visitors` (default 2000), `--concurrency` (default 20) and `--seed` are optional. `/expose` and `/convert` always return 202, so compare the output with `/results` to see what was stored.

## Design highlights

- **Assignment is pure.** Two salted sha256 hashes decide enrolment and variant, so changing traffic never moves an enrolled visitor to a different variant.
- **The page always renders.** The snippet has a 200ms hard timeout, and any failure leaves the default content.
- **Tracking can't be spoofed into another variant.** The server recomputes the variant for every event, and writes are idempotent.
- **Tracking drops rather than blocks.** Writes have a 1s connect limit and a 500ms statement timeout, and a failed write loses that event.
- **The LLM stays off the hot path.** Copy is generated at configure time, stored in the variant and must be approved before an experiment can start.
- **Results flag broken experiments.** A sample ratio mismatch check marks results as untrustworthy.

See [DESIGN.md](DESIGN.md) for the reasoning, measurements and trade-offs.
