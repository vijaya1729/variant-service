import os

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Variant Service")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
def health_db():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return JSONResponse({"status": "error", "detail": "DATABASE_URL not set"}, status_code=500)
    try:
        # prepare_threshold=None: Supabase's transaction pooler doesn't support prepared statements
        with psycopg.connect(url, prepare_threshold=None, connect_timeout=3) as conn:
            conn.execute("select 1")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": type(e).__name__}, status_code=503)
