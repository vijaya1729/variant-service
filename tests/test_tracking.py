import json
import socket
import time
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db, service
from app.assignment import assign
from app.main import app
from app.models import Experiment, Variant

client = TestClient(app)


class StubCache:
    def __init__(self, *experiments: Experiment) -> None:
        self.experiments = {experiment.id: experiment for experiment in experiments}

    def get(self) -> dict[str, Experiment]:
        return self.experiments


def make_experiment(
    experiment_id: str = "exp", traffic: int = 100, status: str = "running"
) -> Experiment:
    variants = [Variant(key="A", weight=50), Variant(key="B", weight=50)]
    return Experiment(
        id=experiment_id, salt="salt", traffic=traffic, variants=variants, status=status
    )


def serve(monkeypatch: pytest.MonkeyPatch, *experiments: Experiment) -> None:
    for experiment in experiments:
        db.create_experiment(experiment)
    running = [experiment for experiment in experiments if experiment.status == "running"]
    monkeypatch.setattr(service, "cache", StubCache(*running))


def count(table: str) -> int:
    with db.connect() as conn:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]


def variant_keys(table: str) -> list[str]:
    with db.connect() as conn:
        return [row[0] for row in conn.execute(f"select variant_key from {table}")]


def post(path: str, **body: object):
    return client.post(path, json=body)


@pytest.mark.parametrize(("path", "table"), [("/expose", "exposures"), ("/convert", "conversions")])
def test_records_the_recomputed_variant(
    path: str, table: str, database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = make_experiment()
    serve(monkeypatch, experiment)

    response = post(path, experiment_id="exp", visitor_id="visitor-1")

    assert response.status_code == 202
    assert variant_keys(table) == [assign("visitor-1", experiment).key]


@pytest.mark.parametrize(("path", "table"), [("/expose", "exposures"), ("/convert", "conversions")])
def test_duplicate_events_make_one_row(
    path: str,
    table: str,
    database: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    serve(monkeypatch, make_experiment())

    for _ in range(3):
        assert post(path, experiment_id="exp", visitor_id="visitor-1").status_code == 202

    assert count(table) == 1
    # Without ON CONFLICT a duplicate would still leave one row, but as a dropped error.
    assert "dropping tracking event" not in caplog.text


def test_conversion_goals_are_counted_separately(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, make_experiment())

    post("/convert", experiment_id="exp", visitor_id="visitor-1", goal="signup")
    post("/convert", experiment_id="exp", visitor_id="visitor-1", goal="purchase")
    post("/convert", experiment_id="exp", visitor_id="visitor-1", goal="purchase")

    assert count("conversions") == 2


def test_conversion_goal_defaults(database: None, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, make_experiment())

    post("/convert", experiment_id="exp", visitor_id="visitor-1")

    with db.connect() as conn:
        assert conn.execute("select goal from conversions").fetchone() == ("default",)


@pytest.mark.parametrize(("path", "table"), [("/expose", "exposures"), ("/convert", "conversions")])
def test_client_sent_variant_is_ignored(
    path: str, table: str, database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = make_experiment()
    serve(monkeypatch, experiment)
    actual = assign("visitor-1", experiment).key
    spoofed = "B" if actual == "A" else "A"

    post(path, experiment_id="exp", visitor_id="visitor-1", variant_key=spoofed, variant=spoofed)

    assert variant_keys(table) == [actual]


@pytest.mark.parametrize(("path", "table"), [("/expose", "exposures"), ("/convert", "conversions")])
@pytest.mark.parametrize(
    "experiment",
    [
        pytest.param(make_experiment("other"), id="unknown"),
        pytest.param(make_experiment(status="paused"), id="not-running"),
        pytest.param(make_experiment(traffic=0), id="not-enrolled"),
    ],
)
def test_ineligible_events_are_ignored(
    path: str, table: str, experiment: Experiment, database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, experiment)

    assert post(path, experiment_id="exp", visitor_id="visitor-1").status_code == 202
    assert count(table) == 0


@pytest.mark.parametrize("path", ["/expose", "/convert"])
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(b'{"experiment_id": "exp"}', id="missing-visitor"),
        pytest.param(b'{"experiment_id": "exp", "visitor_id": ""}', id="empty-visitor"),
    ],
)
def test_malformed_bodies_still_return_202(
    path: str, body: bytes, database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, make_experiment())

    assert client.post(path, content=body).status_code == 202
    assert count("exposures") + count("conversions") == 0


@pytest.mark.parametrize(("path", "table"), [("/expose", "exposures"), ("/convert", "conversions")])
def test_text_plain_bodies_are_accepted(
    path: str, table: str, database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, make_experiment())

    response = client.post(
        path,
        content=json.dumps({"experiment_id": "exp", "visitor_id": "visitor-1"}),
        headers={"Content-Type": "text/plain;charset=UTF-8", "Origin": "https://customer.example"},
    )

    assert response.status_code == 202
    assert response.headers["access-control-allow-origin"] == "*"
    assert count(table) == 1


@pytest.mark.parametrize("path", ["/expose", "/convert"])
def test_json_preflight_is_allowed(path: str) -> None:
    response = client.options(
        path,
        headers={
            "Origin": "https://customer.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize(
    ("path", "writer"),
    [("/expose", "record_exposure"), ("/convert", "record_conversion")],
)
def test_db_error_still_returns_202(
    path: str, writer: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "cache", StubCache(make_experiment()))
    monkeypatch.setattr(db, writer, broken)

    assert post(path, experiment_id="exp", visitor_id="visitor-1").status_code == 202
    assert "dropping tracking event" in caplog.text


@pytest.mark.parametrize("path", ["/expose", "/convert"])
def test_unreachable_db_still_returns_202(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "cache", StubCache(make_experiment()))
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")

    assert post(path, experiment_id="exp", visitor_id="visitor-1").status_code == 202


@pytest.mark.parametrize("path", ["/expose", "/convert"])
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"experiment_id": "exp", "visitor_id": "v"}', id="recorded"),
        pytest.param(b'{"experiment_id": "other", "visitor_id": "v"}', id="ignored"),
        pytest.param(b"not json", id="malformed"),
    ],
)
def test_response_body_is_always_accepted(
    path: str, body: bytes, database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, make_experiment())

    response = client.post(path, content=body)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


@pytest.mark.parametrize("path", ["/expose", "/convert"])
def test_response_body_is_accepted_when_the_db_is_down(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "cache", StubCache(make_experiment()))
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")

    response = post(path, experiment_id="exp", visitor_id="visitor-1")

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


@pytest.mark.parametrize("path", ["/expose", "/convert"])
def test_cache_error_still_returns_202(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenCache:
        def get(self) -> dict[str, Experiment]:
            raise RuntimeError("boom")

    monkeypatch.setattr(service, "cache", BrokenCache())

    assert post(path, experiment_id="exp", visitor_id="visitor-1").status_code == 202


@pytest.fixture
def stalled_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The kernel accepts the TCP connection, then nothing ever answers, like a hung pooler.
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        port = server.getsockname()[1]
        monkeypatch.setenv("DATABASE_URL", f"postgresql://nobody@127.0.0.1:{port}/none")
        yield


def test_tracking_connect_gives_up_after_one_second(stalled_server: None) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        db.record_exposure("exp", "visitor", "A")
    assert time.monotonic() - started < 1.5


def test_tracking_statement_is_cancelled_after_500ms(database: None) -> None:
    db.create_experiment(make_experiment())
    with db.connect() as blocker:
        # Uncommitted, so the tracked insert has to wait on this row's unique key.
        blocker.execute(
            "insert into exposures (experiment_id, visitor_id, variant_key) "
            "values ('exp', 'visitor', 'A')"
        )
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled):
            db.record_exposure("exp", "visitor", "A")
        elapsed = time.monotonic() - started
        blocker.rollback()

    assert 0.4 < elapsed < 1.5
