import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app import db, llm, service
from app.assignment import assign
from app.main import app
from app.models import Experiment, Variant

ADMIN_KEY = "test-admin-key"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}
LLM_KEY = "sk-test-secret-key"
BODY = {"field": "headline", "brief": "Pricing page for small teams", "tone": "plain"}
STATIC_PAYLOAD = {"headline": "Static headline", "image": "hero.png"}
GENERATE_URL = "/experiments/exp/variants/B/generate"
REQUEST = httpx2.Request("POST", "https://llm.test/v1/messages")
RealAnthropic = anthropic.Anthropic

client = TestClient(app)


class FakeAnthropic:
    """Stands in for anthropic.Anthropic, so no test can reach the real API."""

    result: object = None
    init_kwargs: dict = {}
    create_kwargs: dict = {}

    def __init__(self, **kwargs: object) -> None:
        FakeAnthropic.init_kwargs = kwargs
        self.messages = self

    def create(self, **kwargs: object) -> object:
        FakeAnthropic.create_kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def llm_response(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=40, output_tokens=12),
    )


def respond_with(copy: str) -> None:
    FakeAnthropic.result = llm_response(json.dumps({"text": copy}))


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setenv("LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.delenv("LLM_MODEL", raising=False)


def make_experiment(status: str = "draft") -> Experiment:
    variants = [
        Variant(key="A", weight=50),
        Variant(key="B", weight=50, payload=STATIC_PAYLOAD),
    ]
    return Experiment(id="exp", salt="salt", traffic=100, variants=variants, status=status)


@pytest.fixture
def draft(database: None) -> Experiment:
    experiment = make_experiment()
    db.create_experiment(experiment)
    return experiment


def variant_row(key: str = "B") -> tuple:
    with db.connect() as conn:
        return conn.execute(
            """
            select payload, source, approved, generation_meta
            from variants where experiment_id = 'exp' and key = %s
            """,
            (key,),
        ).fetchone()


def set_status(status: str):
    return client.patch(
        "/experiments/exp/status", json={"status": status}, headers=ADMIN_HEADERS
    )


def generate():
    return client.post(GENERATE_URL, json=BODY, headers=ADMIN_HEADERS)


def approve():
    return client.post("/experiments/exp/variants/B/approve", headers=ADMIN_HEADERS)


def test_success_sets_payload_and_leaves_the_variant_unapproved(draft: Experiment) -> None:
    respond_with("Pricing that fits your team")

    response = generate()

    assert response.status_code == 200
    assert response.json()["payload"]["headline"] == "Pricing that fits your team"
    assert response.json()["source"] == "llm"
    assert response.json()["approved"] is False

    payload, source, approved, _ = variant_row()
    assert payload == {"headline": "Pricing that fits your team", "image": "hero.png"}
    assert (source, approved) == ("llm", False)


def test_success_stores_generation_metadata(draft: Experiment) -> None:
    respond_with("Copy")

    generate()

    meta = variant_row()[3]
    assert meta["model"] == "claude-haiku-5-5"
    assert meta["usage"] == {"input_tokens": 40, "output_tokens": 12}
    assert len(meta["prompt_sha256"]) == 64
    assert BODY["brief"] not in json.dumps(meta)


def test_client_uses_the_timeout_key_and_no_retries(
    draft: Experiment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    respond_with("Copy")

    generate()

    assert FakeAnthropic.init_kwargs == {"api_key": LLM_KEY, "timeout": 10.0, "max_retries": 0}
    assert FakeAnthropic.create_kwargs["model"] == "claude-sonnet-5-5"
    assert BODY["brief"] in FakeAnthropic.create_kwargs["messages"][0]["content"]
    assert BODY["tone"] in FakeAnthropic.create_kwargs["messages"][0]["content"]


def test_long_copy_is_trimmed_at_a_word_boundary(draft: Experiment) -> None:
    respond_with("word " * 40)

    headline = generate().json()["payload"]["headline"]

    assert len(headline) <= 120
    assert headline.endswith("word")


def test_long_copy_without_spaces_is_cut_to_the_limit(draft: Experiment) -> None:
    respond_with("x" * 500)
    assert generate().json()["payload"]["headline"] == "x" * 120


def test_whitespace_in_copy_is_collapsed(draft: Experiment) -> None:
    respond_with("  Two\n  lines  ")
    assert generate().json()["payload"]["headline"] == "Two lines"


@pytest.mark.parametrize(
    ("result", "message"),
    [
        pytest.param(anthropic.APITimeoutError(REQUEST), "timed out", id="timeout"),
        pytest.param(anthropic.APIConnectionError(request=REQUEST), "failed", id="connection"),
        pytest.param(
            anthropic.InternalServerError(
                "overloaded", response=httpx2.Response(500, request=REQUEST), body=None
            ),
            "failed",
            id="server-error",
        ),
        pytest.param(
            anthropic.RateLimitError(
                "slow down", response=httpx2.Response(429, request=REQUEST), body=None
            ),
            "failed",
            id="rate-limited",
        ),
        pytest.param(llm_response("not json"), "invalid JSON", id="bad-json"),
        pytest.param(llm_response('{"wrong": "key"}'), "invalid JSON", id="wrong-shape"),
        pytest.param(llm_response('{"text": "   "}'), "empty", id="empty-copy"),
        pytest.param(llm_response("", "refusal"), "stopped early", id="refusal"),
        pytest.param(llm_response('{"te', "max_tokens"), "stopped early", id="truncated"),
    ],
)
def test_failure_returns_502_and_leaves_the_variant_untouched(
    result: object, message: str, draft: Experiment
) -> None:
    FakeAnthropic.result = result
    before = variant_row()

    response = generate()

    assert response.status_code == 502
    assert message in response.json()["detail"]
    assert variant_row() == before
    assert before[:3] == (STATIC_PAYLOAD, "static", True)


def test_experiment_stays_usable_after_a_failure(draft: Experiment) -> None:
    FakeAnthropic.result = anthropic.APITimeoutError(REQUEST)
    generate()

    assert set_status("running").status_code == 200


def test_missing_key_returns_502_without_calling_the_llm(
    draft: Experiment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY")
    FakeAnthropic.init_kwargs = {}

    assert generate().status_code == 502
    assert FakeAnthropic.init_kwargs == {}


def test_key_never_reaches_responses_or_logs(
    draft: Experiment, caplog: pytest.LogCaptureFixture
) -> None:
    FakeAnthropic.result = anthropic.AuthenticationError(
        f"bad key {LLM_KEY}", response=httpx2.Response(401, request=REQUEST), body=None
    )

    with caplog.at_level(logging.DEBUG):
        response = generate()

    assert response.status_code == 502
    assert LLM_KEY not in response.text
    assert LLM_KEY not in caplog.text


def test_generation_response_omits_metadata_and_key(draft: Experiment) -> None:
    respond_with("Copy")

    response = generate()

    assert LLM_KEY not in response.text
    assert "generation_meta" not in response.json()


@pytest.mark.parametrize("status", ["running", "paused", "stopped"])
def test_generating_outside_draft_is_409_and_skips_the_llm(
    status: str, database: None
) -> None:
    db.create_experiment(make_experiment(status))
    FakeAnthropic.init_kwargs = {}

    assert generate().status_code == 409
    assert FakeAnthropic.init_kwargs == {}


def test_experiment_going_live_during_generation_is_409(
    draft: Experiment, monkeypatch: pytest.MonkeyPatch
) -> None:
    def go_live_then_respond(field: str, brief: str, tone: str | None) -> tuple[str, dict]:
        assert db.update_status("exp", "draft", "running")
        return "Too late", {}

    monkeypatch.setattr(llm, "generate_copy", go_live_then_respond)

    assert generate().status_code == 409
    assert variant_row()[0] == STATIC_PAYLOAD


@pytest.mark.parametrize(
    "path",
    ["/experiments/nope/variants/B/generate", "/experiments/exp/variants/nope/generate"],
)
def test_generating_for_unknown_target_is_404(path: str, draft: Experiment) -> None:
    assert client.post(path, json=BODY, headers=ADMIN_HEADERS).status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/experiments/nope/variants/B/approve", "/experiments/exp/variants/nope/approve"],
)
def test_approving_unknown_target_is_404(path: str, draft: Experiment) -> None:
    assert client.post(path, headers=ADMIN_HEADERS).status_code == 404


@pytest.mark.parametrize(
    "path",
    [GENERATE_URL, "/experiments/exp/variants/B/approve"],
)
def test_llm_endpoints_require_the_admin_key(path: str) -> None:
    assert client.post(path, json=BODY).status_code == 401


def test_invalid_generate_body_is_422(draft: Experiment) -> None:
    response = client.post(GENERATE_URL, json={"field": "headline"}, headers=ADMIN_HEADERS)
    assert response.status_code == 422


def test_cannot_start_while_a_variant_is_unapproved(draft: Experiment) -> None:
    respond_with("Copy")
    generate()

    response = set_status("running")

    assert response.status_code == 409
    assert "unapproved" in response.json()["detail"]
    assert db.get_experiment("exp").status == "draft"


def test_db_rejects_start_even_if_the_service_check_is_passed(draft: Experiment) -> None:
    respond_with("Copy")
    generate()

    assert db.update_status("exp", "draft", "running") is False


def test_approve_allows_start_and_serves_the_generated_copy(
    draft: Experiment, monkeypatch: pytest.MonkeyPatch
) -> None:
    respond_with("Approved copy")
    generate()

    response = approve()
    assert response.status_code == 200
    assert response.json()["approved"] is True
    assert set_status("running").status_code == 200

    experiment = db.list_running_experiments()[0]
    visitor = next(
        f"v{i}" for i in range(100) if assign(f"v{i}", experiment).key == "B"
    )
    monkeypatch.setattr(service, "cache", SimpleNamespace(get=lambda: {"exp": experiment}))
    assignments = client.post(
        "/assign", json={"visitor_id": visitor, "experiment_ids": ["exp"]}
    ).json()["assignments"]
    assert assignments["exp"]["payload"]["headline"] == "Approved copy"


def test_regenerating_needs_a_fresh_approval(draft: Experiment) -> None:
    respond_with("First")
    generate()
    approve()

    respond_with("Second")
    generate()

    assert variant_row()[2] is False
    assert set_status("running").status_code == 409


def test_generating_keeps_other_fields_generated_earlier(draft: Experiment) -> None:
    respond_with("A headline")
    generate()
    respond_with("Buy now")
    client.post(
        GENERATE_URL, json=BODY | {"field": "cta"}, headers=ADMIN_HEADERS
    )

    assert variant_row()[0] == {"headline": "A headline", "cta": "Buy now", "image": "hero.png"}


def use_real_sdk(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx2.Request], httpx2.Response]
) -> None:
    # The real client over a mock transport, so the SDK itself checks the request shape
    # and parses the response, with no network involved.
    def real_client(**kwargs: object) -> anthropic.Anthropic:
        http_client = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
        return RealAnthropic(http_client=http_client, **kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", real_client)


def test_real_sdk_sends_the_request_and_parses_the_response(
    draft: Experiment, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "s"},
                    {"type": "text", "text": '{"text": "From the real SDK"}'},
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 9},
            },
        )

    use_real_sdk(monkeypatch, handler)

    response = generate()

    assert response.json()["payload"]["headline"] == "From the real SDK"
    assert variant_row()[3]["usage"] == {"input_tokens": 7, "output_tokens": 9}

    body = json.loads(sent[0].content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert LLM_KEY in str(sent[0].headers.raw)


def test_real_sdk_timeout_makes_one_attempt_and_returns_502(
    draft: Experiment, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        raise httpx2.ReadTimeout("slow", request=request)

    use_real_sdk(monkeypatch, handler)

    response = generate()

    assert response.status_code == 502
    assert "timed out" in response.json()["detail"]
    assert len(attempts) == 1


def test_assign_path_does_not_import_the_sdk() -> None:
    code = "import sys, app.main; sys.exit('anthropic' in sys.modules)"
    root = Path(__file__).parent.parent
    assert subprocess.run([sys.executable, "-c", code], cwd=root).returncode == 0
