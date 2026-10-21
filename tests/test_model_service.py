from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from model_service import ChatMessage, ModelProfile, ModelService, ModelServiceError, ProfileStore
from model_service import client as client_module


def _profile(profile_id: str, model: str) -> ModelProfile:
    return ModelProfile(
        profile_id=profile_id,
        name=profile_id,
        base_url="https://api.example.test/v1",
        model=model,
        api_key="secret-key",
    )


def test_profile_switch_and_chat_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    store.save(_profile("deepseek", "model-a"))
    store.save(_profile("bailian", "model-b"))
    store.activate("deepseek")
    frozen = store.active_profile()
    store.activate("bailian")
    assert frozen.model == "model-a"
    assert store.active_profile().model == "model-b"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({
                "id": "resp-1", "model": "model-a",
                "choices": [{"message": {"content": '{"ok":true}'}}],
            }).encode()

    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    result = ModelService(store).generate([ChatMessage("user", "hello")], profile=frozen)
    assert result.content == '{"ok":true}'
    request, timeout = calls[0]
    assert request.full_url == "https://api.example.test/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer secret-key"
    assert json.loads(request.data)["model"] == "model-a"
    assert json.loads(request.data)["response_format"] == {"type": "json_object"}
    assert timeout == 60.0


def test_invalid_service_reply_has_bounded_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices": []}'

    monkeypatch.setattr(client_module, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    service = ModelService(ProfileStore(tmp_path / "profiles.json"))
    with pytest.raises(ModelServiceError, match="invalid_response"):
        service.generate([ChatMessage("user", "hello")], profile=_profile("one", "model-a"))


def test_remote_api_key_requires_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ModelProfile(
            profile_id="unsafe", name="unsafe", base_url="http://api.example.test/v1",
            model="model-a", api_key="secret-key",
        )


def test_http_failure_does_not_expose_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request, timeout):
        raise HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(client_module, "urlopen", fail)
    with pytest.raises(ModelServiceError) as error:
        ModelService().generate(
            [ChatMessage("user", "hello")], profile=_profile("one", "model-a")
        )
    assert error.value.code == "http_401"
    assert "secret-key" not in str(error.value)
