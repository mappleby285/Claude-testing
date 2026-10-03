"""Shared test fixtures. No test makes a real API call: the Anthropic client is mocked."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx2
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from cardconv.config import Settings  # noqa: E402
from cardconv.extractor import Extractor  # noqa: E402
from cardconv.pipeline import Pipeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data",
        export_dir=tmp_path / "exports",
        watch_stable_seconds=2,
        watch_pair_wait_seconds=10,
    )
    s.ensure_dirs()
    return s


def make_image(text: str = "card", size=(600, 350), color=(255, 255, 255), fmt="PNG", **save_kw) -> bytes:
    img = Image.new("RGB", size, color)
    ImageDraw.Draw(img).text((20, 20), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save_kw)
    return buf.getvalue()


def extraction_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "cards_detected": 1,
        "full_name": "Jordan A. Rivera",
        "prefix": "",
        "given_name": "Jordan",
        "middle_name": "A.",
        "family_name": "Rivera",
        "suffix": "",
        "company": "Northwind Example Logistics, Inc.",
        "department": "",
        "job_title": "Senior Project Manager",
        "emails": ["jordan.rivera@northwind.example.com"],
        "phones": [
            {"label": "work", "number": "+1 (555) 010-4477 ext. 214"},
            {"label": "mobile", "number": "+1 555-010-9921"},
            {"label": "fax", "number": "+1 (555) 010-4478"},
        ],
        "websites": ["www.northwind.example.com"],
        "address": {
            "street": "1200 Harbor Way, Suite 300",
            "city": "Springfield",
            "region": "OR",
            "postal_code": "97477",
            "country": "USA",
        },
        "other_text": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def text_response(payload: Any, stop_reason: str = "end_turn") -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        model="claude-opus-5-5",
        _request_id="req_test",
    )


_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def api_error(cls: type, status: int, message: str = "error", headers: dict | None = None) -> Exception:
    response = httpx2.Response(status, request=_REQ, headers=headers or {})
    return cls(message, response=response, body={"error": {"message": message}})


def connection_error() -> Exception:
    return anthropic.APIConnectionError(request=_REQ)


class FakeClient:
    """Mimics client.beta.messages.create. `script` items are responses or exceptions;
    a callable item is called with the request kwargs."""

    def __init__(self, script: list[Any] | None = None, default: Any = None):
        self.script = list(script or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else self.default
        if callable(item) and not isinstance(item, SimpleNamespace):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        if item is None:
            raise AssertionError("FakeClient script exhausted")
        return item


@pytest.fixture
def fake_client_factory() -> Callable[..., FakeClient]:
    return FakeClient


@pytest.fixture
def make_pipeline(settings: Settings) -> Callable[..., tuple[Pipeline, FakeClient]]:
    def _make(script: list[Any] | None = None, default: Any = None, mode: str | None = None):
        client = FakeClient(script, default)
        extractor = Extractor(settings, client=client, sleep=lambda s: None)
        pipeline = Pipeline(settings, extractor=extractor)
        if mode:
            pipeline.set_export_mode(mode)
        return pipeline, client

    return _make
