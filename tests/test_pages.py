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


def test_demo_is_html_with_default_content() -> None:
    response = client.get("/demo")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Ship better pages, one test at a time" in response.text


def test_styles_are_served_as_css() -> None:
    response = client.get("/styles.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
