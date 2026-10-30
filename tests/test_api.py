import pytest
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import db, service
from app.assignment import assign
from app.main import app
from app.models import Experiment, Variant

ADMIN_KEY = "test-admin-key"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}
ORIGIN_HEADERS = {"Origin": "https://customer.example"}

client = TestClient(app)


class StubCache:
    def __init__(self, *experiments: Experiment) -> None:
        self.experiments = {experiment.id: experiment for experiment in experiments}

    def get(self) -> dict[str, Experiment]:
        return self.experiments


class BrokenCache:
    def get(self) -> dict[str, Experiment]:
        raise RuntimeError("boom")


def make_experiment(
    experiment_id: str = "exp", status: str = "running", traffic: int = 100
) -> Experiment:
    variants = [
        Variant(key="A", weight=50, payload={"headline": "a"}),
        Variant(key="B", weight=50, payload={"headline": "b"}),
    ]
    return Experiment(
        id=experiment_id, salt="salt", traffic=traffic, variants=variants, status=status
    )


def experiment_body(**overrides: object) -> dict:
    body = {
        "id": "exp",
        "traffic": 100,
        "variants": [{"key": "A", "weight": 50}, {"key": "B", "weight": 50}],
    }
    return body | overrides


@pytest.fixture(autouse=True)
def admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Experiment]:
    experiments: dict[str, Experiment] = {}

    def create_experiment(experiment: Experiment) -> None:
        if experiment.id in experiments:
            raise UniqueViolation()
        experiments[experiment.id] = experiment

    def update_status(experiment_id: str, from_status: str, to_status: str) -> bool:
        experiment = experiments[experiment_id]
        if experiment.status != from_status:
            return False
        experiments[experiment_id] = experiment.model_copy(update={"status": to_status})
        return True

    monkeypatch.setattr(db, "create_experiment", create_experiment)
    monkeypatch.setattr(db, "get_experiment", experiments.get)
    monkeypatch.setattr(db, "update_status", update_status)
    return experiments


def post_assign(experiment_ids: list[str], visitor_id: str = "visitor-1") -> dict:
    response = client.post(
        "/assign", json={"visitor_id": visitor_id, "experiment_ids": experiment_ids}
    )
    assert response.status_code == 200
    return response.json()["assignments"]


def test_assign_returns_the_core_variant_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    experiment = make_experiment()
    monkeypatch.setattr(service, "cache", StubCache(experiment))
    expected = assign("visitor-1", experiment)

    assert post_assign(["exp"]) == {
        "exp": {"variant": expected.key, "payload": expected.payload}
    }


def test_assign_nulls_unknown_paused_and_not_enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = StubCache(
        make_experiment("live"),
        make_experiment("paused", status="paused"),
        make_experiment("held-back", traffic=0),
    )
    monkeypatch.setattr(service, "cache", cache)

    assignments = post_assign(["live", "paused", "held-back", "unknown"])

    assert assignments["live"] is not None
    assert assignments["paused"] is None
    assert assignments["held-back"] is None
    assert assignments["unknown"] is None


def test_assign_returns_all_nulls_when_the_cache_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "cache", BrokenCache())
    assert post_assign(["one", "two"]) == {"one": None, "two": None}


def test_assign_returns_all_nulls_when_assignment_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_assign(visitor_id: str, experiment: Experiment) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "cache", StubCache(make_experiment("one")))
    monkeypatch.setattr(service, "assign", broken_assign)

    assert post_assign(["one", "two"]) == {"one": None, "two": None}


def test_assign_never_touches_the_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> None:
        raise AssertionError("assign must not touch the DB")

    monkeypatch.setattr(db, "connect", forbidden)
    monkeypatch.setattr(service, "cache", StubCache(make_experiment()))

    assert post_assign(["exp"])["exp"] is not None


def test_assign_allows_any_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "cache", StubCache())
    body = {"visitor_id": "v", "experiment_ids": []}

    response = client.post("/assign", json=body, headers=ORIGIN_HEADERS)
    preflight = client.options(
        "/assign",
        headers=ORIGIN_HEADERS
        | {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.headers["access-control-allow-origin"] == "*"
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"


def test_admin_endpoints_have_no_cors_headers(fake_db: dict[str, Experiment]) -> None:
    fake_db["exp"] = make_experiment()
    response = client.get("/experiments/exp", headers=ADMIN_HEADERS | ORIGIN_HEADERS)

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/experiments"),
        ("get", "/experiments/exp"),
        ("patch", "/experiments/exp/status"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [pytest.param({}, id="missing"), pytest.param({"X-Admin-Key": "wrong"}, id="wrong")],
)
def test_admin_endpoints_require_the_key(method: str, path: str, headers: dict) -> None:
    assert client.request(method, path, headers=headers, json={}).status_code == 401


@pytest.mark.parametrize("configured", [None, ""])
def test_admin_is_locked_when_no_key_is_configured(
    monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    if configured is None:
        monkeypatch.delenv("ADMIN_API_KEY")
    else:
        monkeypatch.setenv("ADMIN_API_KEY", configured)

    assert client.get("/experiments/exp").status_code == 401
    assert client.get("/experiments/exp", headers={"X-Admin-Key": ""}).status_code == 401


def test_create_defaults_to_draft_and_generates_a_salt(fake_db: dict[str, Experiment]) -> None:
    response = client.post("/experiments", json=experiment_body(), headers=ADMIN_HEADERS)

    assert response.status_code == 201
    assert response.json()["status"] == "draft"
    assert response.json()["salt"]
    assert fake_db["exp"].salt == response.json()["salt"]


def test_create_keeps_a_given_salt(fake_db: dict[str, Experiment]) -> None:
    body = experiment_body(salt="mine")
    response = client.post("/experiments", json=body, headers=ADMIN_HEADERS)
    assert response.json()["salt"] == "mine"


def test_generated_salts_differ(fake_db: dict[str, Experiment]) -> None:
    salts = {
        client.post(
            "/experiments", json=experiment_body(id=experiment_id), headers=ADMIN_HEADERS
        ).json()["salt"]
        for experiment_id in ("one", "two")
    }
    assert len(salts) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"status": "running"}, id="not-draft"),
        pytest.param({"traffic": 101}, id="bad-traffic"),
        pytest.param({"variants": [{"key": "A", "weight": 100}]}, id="single-variant"),
    ],
)
def test_create_rejects_invalid_config(overrides: dict, fake_db: dict[str, Experiment]) -> None:
    body = experiment_body(**overrides)
    assert client.post("/experiments", json=body, headers=ADMIN_HEADERS).status_code == 422
    assert fake_db == {}


def test_create_rejects_a_duplicate_id(fake_db: dict[str, Experiment]) -> None:
    fake_db["exp"] = make_experiment()
    response = client.post("/experiments", json=experiment_body(), headers=ADMIN_HEADERS)
    assert response.status_code == 409


def test_get_returns_the_experiment(fake_db: dict[str, Experiment]) -> None:
    fake_db["exp"] = make_experiment()
    response = client.get("/experiments/exp", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json()["id"] == "exp"


def test_get_unknown_is_404(fake_db: dict[str, Experiment]) -> None:
    assert client.get("/experiments/nope", headers=ADMIN_HEADERS).status_code == 404


def patch_status(status: str, experiment_id: str = "exp"):
    return client.patch(
        f"/experiments/{experiment_id}/status", json={"status": status}, headers=ADMIN_HEADERS
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "running"),
        ("running", "paused"),
        ("paused", "running"),
        ("running", "stopped"),
        ("paused", "stopped"),
    ],
)
def test_valid_transitions(current: str, target: str, fake_db: dict[str, Experiment]) -> None:
    fake_db["exp"] = make_experiment(status=current)

    response = patch_status(target)

    assert response.status_code == 200
    assert response.json()["status"] == target
    assert fake_db["exp"].status == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "draft"),
        ("draft", "paused"),
        ("draft", "stopped"),
        ("running", "running"),
        ("running", "draft"),
        ("paused", "paused"),
        ("paused", "draft"),
        ("stopped", "draft"),
        ("stopped", "running"),
        ("stopped", "paused"),
        ("stopped", "stopped"),
    ],
)
def test_invalid_transitions(current: str, target: str, fake_db: dict[str, Experiment]) -> None:
    fake_db["exp"] = make_experiment(status=current)

    assert patch_status(target).status_code == 409
    assert fake_db["exp"].status == current


def test_transition_lost_to_a_concurrent_change_is_rejected(
    fake_db: dict[str, Experiment], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_db["exp"] = make_experiment(status="draft")
    monkeypatch.setattr(db, "update_status", lambda *args: False)

    assert patch_status("running").status_code == 409


def test_status_of_unknown_experiment_is_404(fake_db: dict[str, Experiment]) -> None:
    assert patch_status("running", "nope").status_code == 404


def test_unknown_status_is_rejected(fake_db: dict[str, Experiment]) -> None:
    fake_db["exp"] = make_experiment()
    assert patch_status("archived").status_code == 422


def test_there_is_no_endpoint_for_changing_variants(fake_db: dict[str, Experiment]) -> None:
    fake_db["exp"] = make_experiment()
    for method in ("put", "patch"):
        response = client.request(
            method, "/experiments/exp", json={"variants": []}, headers=ADMIN_HEADERS
        )
        assert response.status_code == 405
