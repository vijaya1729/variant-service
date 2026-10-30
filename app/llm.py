import hashlib
import logging
import os

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"
TIMEOUT_SECONDS = 10.0
MAX_COPY_LENGTH = 120
# Well above the copy itself, because thinking on current models counts towards this too.
MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You write short marketing copy for website A/B tests. "
    "Write one option for the requested field, under 100 characters, as plain text."
)
RESPONSE_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}


class GenerationFailed(Exception):
    """Messages reach admin API clients, so they must never quote the upstream error."""


class GeneratedCopy(BaseModel):
    text: str


def _build_prompt(field: str, brief: str, tone: str | None) -> str:
    prompt = f"Field: {field}\nBrief: {brief}"
    return f"{prompt}\nTone: {tone}" if tone else prompt


def _trim(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_COPY_LENGTH:
        return text
    return text[: MAX_COPY_LENGTH + 1].rsplit(" ", 1)[0][:MAX_COPY_LENGTH]


def _parse_copy(response: object) -> str:
    if response.stop_reason != "end_turn":
        raise GenerationFailed(f"LLM stopped early ({response.stop_reason})")
    raw = next((block.text for block in response.content if block.type == "text"), "")
    try:
        text = _trim(GeneratedCopy.model_validate_json(raw).text)
    except ValidationError as exc:
        raise GenerationFailed("LLM returned invalid JSON") from exc
    if not text:
        raise GenerationFailed("LLM returned empty copy")
    return text


def generate_copy(field: str, brief: str, tone: str | None) -> tuple[str, dict]:
    """Returns the copy and its generation metadata, or raises GenerationFailed."""
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise GenerationFailed("LLM_API_KEY is not configured")

    # Imported here so the SDK adds nothing to cold starts on the assign path.
    import anthropic

    model = os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    prompt = _build_prompt(field, brief, tone)
    client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": RESPONSE_FORMAT},
        )
    except anthropic.APITimeoutError as exc:
        raise GenerationFailed("LLM request timed out") from exc
    except anthropic.APIError as exc:
        # Only the class name: upstream error text isn't guaranteed to be free of secrets.
        logger.warning("LLM request failed: %s", type(exc).__name__)
        raise GenerationFailed("LLM request failed") from exc

    prompt_hash = hashlib.sha256(f"{SYSTEM_PROMPT}\n{prompt}".encode()).hexdigest()
    return _parse_copy(response), {
        "model": model,
        "field": field,
        "prompt_sha256": prompt_hash,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }
