from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).parent / "static"

pages = APIRouter(include_in_schema=False)


@pages.get("/snippet.js")
def snippet() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "snippet.js",
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )
