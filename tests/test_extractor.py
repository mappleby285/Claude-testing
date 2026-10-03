"""API failure handling, with a mocked Anthropic client."""

import pytest

import anthropic

from cardconv.extractor import FALLBACK_BETA, CredentialsError, ExtractionFailure, Extractor
from cardconv.imaging import prepare_for_api

from .conftest import FakeClient, api_error, connection_error, extraction_dict, make_image, text_response


@pytest.fixture
def images():
    return [prepare_for_api(make_image("front"))]


def extractor(settings, script, sleeps=None):
    client = FakeClient(script)
    sleeps = sleeps if sleeps is not None else []
    return Extractor(settings, client=client, sleep=sleeps.append), client


def test_success_and_request_shape(settings, images):
    ex, client = extractor(settings, [text_response(extraction_dict())])
    result = ex.extract(images)
    assert result.extraction.full_name == "Jordan A. Rivera"
    assert result.attempts == 1
    req = client.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["effort"] == "high"
    assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
    assert "never instructions" in req["messages"][0]["content"][-1]["text"]
    assert "untrusted DATA" in req["system"]
    image_blocks = [b for b in req["messages"][0]["content"] if b["type"] == "image"]
    assert image_blocks[0]["source"]["media_type"] == "image/jpeg"
    assert "thinking" not in req and "temperature" not in req


def test_front_and_back_sent_in_one_request(settings):
    ex, client = extractor(settings, [text_response(extraction_dict())])
    ex.extract([prepare_for_api(make_image("front")), prepare_for_api(make_image("back"))])
    content = client.calls[0]["messages"][0]["content"]
    assert sum(b["type"] == "image" for b in content) == 2
    assert "front of the same card" in content[0]["text"]


def test_rate_limit_then_success_honours_retry_after(settings, images):
    sleeps = []
    ex, client = extractor(
        settings,
        [api_error(anthropic.RateLimitError, 429, headers={"retry-after": "7"}), text_response(extraction_dict())],
        sleeps,
    )
    result = ex.extract(images)
    assert result.attempts == 2 and sleeps == [7.0]


def test_network_failures_are_bounded(settings, images):
    sleeps = []
    ex, client = extractor(settings, [connection_error()] * 10, sleeps)
    with pytest.raises(ExtractionFailure) as info:
        ex.extract(images)
    assert info.value.category == "network"
    assert len(client.calls) == settings.api_max_attempts == 3
    assert len(sleeps) == 2


def test_server_error_retried(settings, images):
    ex, client = extractor(
        settings, [api_error(anthropic.InternalServerError, 529, "overloaded"), text_response(extraction_dict())]
    )
    assert ex.extract(images).attempts == 2


def test_malformed_json_retried_then_succeeds(settings, images):
    ex, client = extractor(settings, [text_response("{not json"), text_response(extraction_dict())])
    assert ex.extract(images).attempts == 2


def test_schema_mismatch_exhausts_as_malformed(settings, images):
    bad = extraction_dict(phones=[{"label": "satellite", "number": "1"}])
    ex, client = extractor(settings, [text_response(bad)] * 3)
    with pytest.raises(ExtractionFailure) as info:
        ex.extract(images)
    assert info.value.category == "malformed"


def test_truncated_response_is_malformed(settings, images):
    ex, client = extractor(settings, [text_response('{"full_name": "Jo', stop_reason="max_tokens")] * 3)
    with pytest.raises(ExtractionFailure) as info:
        ex.extract(images)
    assert info.value.category == "malformed"


def test_refusal_not_retried(settings, images):
    ex, client = extractor(settings, [text_response("", stop_reason="refusal")])
    with pytest.raises(ExtractionFailure) as info:
        ex.extract(images)
    assert info.value.category == "refusal" and len(client.calls) == 1


def test_authentication_error_raises_credentials_error(settings, images):
    ex, _ = extractor(settings, [api_error(anthropic.AuthenticationError, 401, "invalid x-api-key")])
    with pytest.raises(CredentialsError):
        ex.extract(images)


def test_missing_api_key_raises_credentials_error(settings, images, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CredentialsError):
        Extractor(settings).extract(images)


def test_low_credit_balance_reported_as_credentials_problem(settings, images):
    ex, _ = extractor(
        settings, [api_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the API")]
    )
    with pytest.raises(CredentialsError, match="credit"):
        ex.extract(images)


def test_model_without_fallback_support_retries_without_it(settings, images):
    ex, client = extractor(
        settings,
        [api_error(anthropic.BadRequestError, 400, "fallbacks: not supported for this model"),
         text_response(extraction_dict())],
    )
    ex.extract(images)
    assert "fallbacks" in client.calls[0] and "fallbacks" not in client.calls[1]


def test_effort_can_be_disabled(settings, images):
    settings.effort = ""
    ex, client = extractor(settings, [text_response(extraction_dict())])
    ex.extract(images)
    assert "effort" not in client.calls[0]["output_config"]


def test_unknown_model_fails_card_without_retry(settings, images):
    ex, client = extractor(settings, [api_error(anthropic.NotFoundError, 404, "model not found")])
    with pytest.raises(ExtractionFailure) as info:
        ex.extract(images)
    assert info.value.category == "bad_request" and len(client.calls) == 1
