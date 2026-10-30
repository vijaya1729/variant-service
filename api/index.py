# Vercel entrypoint: exposes the FastAPI app as a serverless function.
from app.main import app  # noqa: F401
