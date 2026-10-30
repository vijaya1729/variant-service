import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app import db
from app.routes import admin, public

PUBLIC_PATHS = {"/assign"}


class PublicCORSMiddleware(CORSMiddleware):
    """CORSMiddleware applies to a whole app; the admin API stays same-origin."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app = FastAPI(title="Variant Service")
app.add_middleware(
    PublicCORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
    # JSON POSTs preflight; caching that keeps it off the page-render path for repeat views.
    max_age=86400,
)
app.include_router(admin)
app.include_router(public)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
def health_db():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return JSONResponse({"status": "error", "detail": "DATABASE_URL not set"}, status_code=500)
    try:
        with db.connect() as conn:
            conn.execute("select 1")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": type(e).__name__}, status_code=503)
