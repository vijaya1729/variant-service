from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_snippet_is_served_as_cacheable_javascript() -> None:
    response = client.get("/snippet.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "public" in response.headers["cache-control"]


def test_snippet_never_uses_inner_html() -> None:
    assert "innerHTML" not in client.get("/snippet.js").text
