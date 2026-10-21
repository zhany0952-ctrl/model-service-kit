"""配置及凭据测试只使用临时文件和假后端，不访问用户凭据库。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, asdict, replace

import httpx
import pytest

from model_service import ChatMessage, ModelProfile, ModelService, ProfileStore
from model_service.config import LegacyConfigError
from model_service.credentials import CredentialError, CredentialStore


class FakeBackend:
    def __init__(self):
        self.values = {}
        self.fail = False

    def set_password(self, service, name, value):
        if self.fail:
            raise RuntimeError("backend-secret")
        self.values[service, name] = value

    def get_password(self, service, name):
        if self.fail:
            raise RuntimeError("backend-secret")
        return self.values.get((service, name))

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


def model(**changes):
    return replace(ModelProfile("one", "本地或网络", "https://example.test/v1", "model"), **changes)


@pytest.fixture
def storage(tmp_path):
    backend = FakeBackend()
    credentials = CredentialStore(backend=backend)
    return ProfileStore(tmp_path / "profiles.json", credentials=credentials), backend


def test_secure_save_edit_activate_and_frozen_snapshot(storage):
    store, backend = storage
    saved = store.save(model(), api_key="first-secret")
    store.activate("one")
    frozen = store.active_profile()
    changed = store.save(replace(saved, model="new-model"), api_key="second-secret")
    assert changed.credential_ref != frozen.credential_ref
    assert store.active_profile().model == "new-model" and frozen.model == "model"
    assert store.credentials.get(frozen.credential_ref) == "first-secret"
    assert store.credentials.get(changed.credential_ref) == "second-secret"
    text = store.path.read_text(encoding="utf-8")
    assert "first-secret" not in text and "second-secret" not in text
    assert "api_key" not in text and "api_key" not in asdict(frozen)
    assert "secret" not in repr(frozen) and len(backend.values) == 2
    with pytest.raises(FrozenInstanceError):
        frozen.model = "changed"


def test_session_key_never_persists_and_other_process_cannot_resolve(storage):
    store, backend = storage
    saved = store.save(model(), api_key="session-secret", credential_mode="session")
    assert store.credentials.get(saved.credential_ref) == "session-secret"
    assert not backend.values and "session-secret" not in store.path.read_text(encoding="utf-8")
    with pytest.raises(CredentialError, match="重新输入"):
        CredentialStore(backend=backend).get(saved.credential_ref)


def test_system_store_failure_does_not_silently_fallback(storage):
    store, backend = storage
    backend.fail = True
    with pytest.raises(CredentialError) as error:
        store.save(model(), api_key="private-secret")
    assert "backend-secret" not in str(error.value) and "private-secret" not in str(error.value)
    assert not store.path.exists()
    saved = store.save(model(), api_key="private-secret", credential_mode="session")
    assert saved.credential_ref.startswith("session:")


def test_local_without_key_and_delete_active_configuration(storage):
    store, _ = storage
    local = model(base_url="http://127.0.0.1:11434/v1")
    store.save(local)
    store.activate("one")
    frozen = store.active_profile()
    assert frozen.credential_ref is None
    store.delete("one")
    assert store.list_profiles() == []
    with pytest.raises(ValueError, match="活动"):
        store.active_profile()
    assert frozen.model == "model"


def test_edit_without_key_preserves_reference_and_clear_is_explicit(storage):
    store, _ = storage
    saved = store.save(model(), api_key="secret")
    edited = store.save(replace(saved, name="改名"))
    assert edited.credential_ref == saved.credential_ref
    cleared = store.save(edited, clear_key=True)
    assert cleared.credential_ref is None
    assert store.credentials.get(saved.credential_ref) == "secret"


def test_legacy_requires_explicit_migration_and_removes_plaintext(storage):
    store, _ = storage
    original = {
        "active_profile_id": "one",
        "profiles": [
            {
                "profile_id": "one",
                "name": "旧配置",
                "base_url": "https://example.test/v1",
                "model": "old-model",
                "api_key": "legacy-secret",
                "timeout_seconds": 35,
                "json_mode": True,
            }
        ],
    }
    store.path.write_text(json.dumps(original), encoding="utf-8")
    before = store.path.read_bytes()
    assert store.is_legacy()
    with pytest.raises(LegacyConfigError):
        store.list_profiles()
    assert store.path.read_bytes() == before
    assert store.migrate_legacy(credential_mode="system") == 1
    assert not store.is_legacy()
    frozen = store.active_profile()
    assert frozen.model == "old-model" and frozen.read_timeout_seconds == 35
    assert store.credentials.get(frozen.credential_ref) == "legacy-secret"
    text = store.path.read_text(encoding="utf-8")
    assert "legacy-secret" not in text and "api_key" not in text


def test_failed_migration_preserves_original_file(storage):
    store, backend = storage
    store.path.write_text(
        json.dumps(
            {
                "active_profile_id": "one",
                "profiles": [
                    {
                        "profile_id": "one",
                        "name": "旧配置",
                        "base_url": "https://example.test/v1",
                        "model": "model",
                        "api_key": "legacy-secret",
                    }
                ],
            }
        )
    )
    before = store.path.read_bytes()
    backend.fail = True
    with pytest.raises(CredentialError):
        store.migrate_legacy(credential_mode="system")
    assert store.path.read_bytes() == before and store.is_legacy()


def test_failed_metadata_write_cleans_only_new_credential(storage, monkeypatch):
    store, backend = storage
    old = store.save(model(), api_key="existing-secret")
    before = dict(backend.values)

    def fail(_):
        raise OSError("模拟写入失败")

    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(OSError):
        store.save(old, api_key="new-secret")
    assert backend.values == before


@pytest.mark.parametrize(
    "changes",
    [
        {"temperature": -1},
        {"temperature": True},
        {"temperature": float("nan")},
        {"temperature": 3},
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"connect_timeout_seconds": 0},
        {"read_timeout_seconds": float("inf")},
        {"token_limit_field": "messages"},
        {"thinking_json": "[]"},
        {"thinking_json": '{"stream":false}'},
        {"thinking_json": '{"api_key":"not-allowed"}'},
        {"thinking_json": '{"thinking":{"password":"not-allowed"}}'},
        {"thinking_json": '{"budget":NaN}'},
        {"thinking_json": '{"x":1,"x":2}'},
        {"base_url": "https://user:secret@example.test/v1"},
    ],
)
def test_invalid_configuration_rejected(changes):
    with pytest.raises(ValueError):
        model(**changes)


def test_optional_parameters_are_omitted_or_explicit_and_immutable():
    assert model().generation_parameters() == {}
    profile = model(
        temperature=0.4, max_output_tokens=800, thinking_json='{"reasoning_effort":"low"}'
    )
    assert profile.generation_parameters() == {
        "temperature": 0.4,
        "max_completion_tokens": 800,
        "reasoning_effort": "low",
    }
    parameters = profile.generation_parameters()
    parameters["reasoning_effort"] = "high"
    assert profile.generation_parameters()["reasoning_effort"] == "low"
    assert model(max_output_tokens=500, token_limit_field="max_tokens").generation_parameters() == {
        "max_tokens": 500,
    }


def test_legacy_constructor_key_becomes_only_session_reference():
    profile = ModelProfile(
        "old", "旧调用兼容", "https://example.test/v1", "m", api_key="legacy-key"
    )
    assert profile.credential_ref.startswith("session:")
    assert "legacy-key" not in repr(profile) and "api_key" not in asdict(profile)


def test_omitting_reference_on_edit_preserves_existing_key(storage):
    store, _ = storage
    saved = store.save(model(), api_key="old-key")
    edited = store.save(model(name="仅编辑名称"))
    assert edited.credential_ref == saved.credential_ref


@pytest.mark.parametrize(
    "extra",
    [
        {"api_key": "secret"},
        {"unexpected": "value"},
    ],
)
def test_new_format_rejects_unknown_root_fields(storage, extra):
    store, _ = storage
    store.save(model())
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data.update(extra)
    store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="根字段"):
        store.list_profiles()


def test_thinking_number_overflow_is_rejected():
    with pytest.raises(ValueError):
        model(thinking_json='{"thinking":{"budget":1e999}}')


def test_backend_selection_rejects_plaintext_plugins(monkeypatch):
    import keyring

    class Plaintext:
        def set_password(self, *_):
            pytest.fail("不应调用明文后端")

    monkeypatch.setattr(keyring, "get_keyring", lambda: Plaintext())
    with pytest.raises(CredentialError):
        CredentialStore().put("not-for-a-file", mode="system")


def test_freeze_config_and_credentials_through_actual_request_construction(storage):
    async def run():
        store, _ = storage
        saved = store.save(
            model(
                temperature=0.2, max_output_tokens=700, thinking_json='{"reasoning_effort":"low"}'
            ),
            api_key="first-key",
        )
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"choices":[{"index":0,"delta":{"content":"内容"},'
                    '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                ).encode(),
            )

        service = ModelService(store, transport=httpx.MockTransport(handler))
        handle = service.start([ChatMessage("user", "背景")], profile=saved)
        store.save(model(model="another", temperature=1), api_key="second-key")
        events = [event async for event in handle.events()]
        assert events[-1]["reason"] == "normal" and len(calls) == 1
        request = calls[0]
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer first-key"
        assert body["model"] == "model" and body["temperature"] == 0.2
        assert body["max_completion_tokens"] == 700 and body["reasoning_effort"] == "low"
        assert request.extensions["timeout"]["connect"] == 10
        assert request.extensions["timeout"]["read"] == 60

    asyncio.run(run())


def test_missing_session_key_fails_before_sending_network_request(storage):
    async def run():
        store, backend = storage
        saved = store.save(model(), api_key="temporary", credential_mode="session")
        calls = []

        def handler(request):
            calls.append(request)
            raise AssertionError("凭据缺失时不应访问网络")

        service = ModelService(
            transport=httpx.MockTransport(handler), credentials=CredentialStore(backend=backend)
        )
        handle = service.start([ChatMessage("user", "背景")], profile=saved)
        events = [event async for event in handle.events()]
        assert events == [
            {"type": "fault", "code": "protocol", "message": "模型凭据不可用，请重新配置。"}
        ]
        assert not calls and "temporary" not in repr(events)

    asyncio.run(run())


def test_failed_migration_metadata_write_rolls_back_new_keys(storage, monkeypatch):
    store, backend = storage
    store.path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "profile_id": "old",
                        "name": "旧",
                        "base_url": "https://example.test/v1",
                        "model": "m",
                        "api_key": "old-secret",
                    }
                ],
                "active_profile_id": "old",
            }
        ),
        encoding="utf-8",
    )
    before = store.path.read_bytes()

    def fail(_):
        raise OSError("写入失败")

    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(OSError):
        store.migrate_legacy(credential_mode="system")
    assert store.path.read_bytes() == before and not backend.values
