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


@pages.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "styles.css",
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@pages.get("/demo")
def demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html", media_type="text/html")
