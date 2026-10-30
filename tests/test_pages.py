import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/snippet.js", "text/javascript"),
        ("/styles.css", "text/css"),
        ("/demo", "text/html"),
        ("/dashboard", "text/html"),
    ],
)
def test_page_is_served_with_the_right_content_type(path: str, content_type: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)


def test_snippet_is_cacheable() -> None:
    assert "public" in client.get("/snippet.js").headers["cache-control"]


@pytest.mark.parametrize("path", ["/snippet.js", "/demo", "/dashboard"])
def test_pages_never_use_inner_html(path: str) -> None:
    assert "innerHTML" not in client.get(path).text


def test_demo_has_default_content_without_javascript() -> None:
    assert "Ship better pages, one test at a time" in client.get("/demo").text


def test_dashboard_inputs_cannot_be_submitted_into_the_url() -> None:
    assert " name=" not in client.get("/dashboard").text.split("<script>")[0].split("</head>")[1]
