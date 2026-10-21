"""不可变模型配置、无明文配置文件与显式旧格式迁移。"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import InitVar, asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from .credentials import DEFAULT_CREDENTIALS, REFERENCE, CredentialStore


class LegacyConfigError(ValueError):
    pass


def _finite_positive(value, label):
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label}必须是正的有限秒数。")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("JSON中有重复字段。")
        result[name] = value
    return result


def _invalid_constant(_):
    raise ValueError("配置必须使用标准JSON数值。")


def _json(text):
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)

        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("配置数值不能溢出。")
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)

        finite(value)
        return value
    except (ValueError, TypeError):
        raise ValueError("配置JSON格式无效，不显示配置原文。") from None


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    name: str
    base_url: str
    model: str
    api_key: InitVar[str] = ""  # 仅兼容旧构造调用；值转为进程凭据，不成为快照字段。
    timeout_seconds: float = 60.0
    json_mode: bool = True  # 仅旧generate使用；新流式入口不发送整份JSON模式。
    credential_ref: str | None = None
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    token_limit_field: str = "max_completion_tokens"
    thinking_json: str = ""

    def __post_init__(self, api_key: str) -> None:
        if any(
            not isinstance(v, str) or not v.strip()
            for v in (self.profile_id, self.name, self.model, self.base_url)
        ):
            raise ValueError("配置标识、显示名称、服务地址和模型名称不能为空。")
        try:
            url = urlparse(self.base_url)
            valid_url = url.scheme in {"http", "https"} and url.hostname and url.port != 0
        except ValueError:
            valid_url = False
        if not valid_url or url.username or url.password or url.query or url.fragment:
            raise ValueError("服务地址须为不含凭据、查询或片段的http(s)根地址。")
        if not isinstance(api_key, str):
            raise ValueError("密钥必须是字符串。")
        if api_key and self.credential_ref:
            raise ValueError("不能同时提供密钥及凭据引用。")
        if self.credential_ref is not None and (
            not isinstance(self.credential_ref, str) or not REFERENCE.fullmatch(self.credential_ref)
        ):
            raise ValueError("凭据引用无效。")
        if (
            (api_key or self.credential_ref)
            and url.scheme == "http"
            and url.hostname
            not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
        ):
            raise ValueError("带API Key的远程服务必须使用HTTPS。")
        _finite_positive(self.timeout_seconds, "旧同步超时")
        _finite_positive(self.connect_timeout_seconds, "连接超时")
        read = (
            self.timeout_seconds if self.read_timeout_seconds is None else self.read_timeout_seconds
        )
        _finite_positive(read, "读取空闲超时")
        object.__setattr__(self, "read_timeout_seconds", read)
        if type(self.json_mode) is not bool:
            raise ValueError("旧JSON模式必须为布尔值。")
        if self.temperature is not None and (
            type(self.temperature) not in {int, float}
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("温度须为0至2之间的有限数值，或留空。")
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens <= 0
        ):
            raise ValueError("输出上限须为正整数，或留空。")
        if self.token_limit_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("输出上限字段不受支持。")
        if not isinstance(self.thinking_json, str):
            raise ValueError("思考设置须为JSON对象文本。")
        settings = _json(self.thinking_json) if self.thinking_json.strip() else {}
        if not isinstance(settings, dict):
            raise ValueError("思考设置须为JSON对象。")
        reserved = {
            "model",
            "messages",
            "stream",
            "stream_options",
            "response_format",
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "n",
            "tools",
            "functions",
        }
        if reserved.intersection(settings):
            raise ValueError("思考设置不能覆盖消息、流协议、工具或独立配置字段。")

        def check_secrets(value):
            if isinstance(value, dict):
                for name, item in value.items():
                    normalized = name.lower().replace("_", "").replace("-", "").replace(" ", "")
                    if normalized in {
                        "apikey",
                        "authorization",
                        "password",
                        "secret",
                        "accesstoken",
                        "credentialref",
                        "headers",
                        "extraheaders",
                    }:
                        raise ValueError("思考参数不能携带凭据、认证头或密钥。")
                    check_secrets(item)
            elif isinstance(value, list):
                for item in value:
                    check_secrets(item)

        check_secrets(settings)
        object.__setattr__(self, "thinking_json", json.dumps(settings, ensure_ascii=False))
        if api_key:
            object.__setattr__(
                self, "credential_ref", DEFAULT_CREDENTIALS.put(api_key, mode="session")
            )

    def generation_parameters(self) -> dict:
        result = _json(self.thinking_json)
        if self.temperature is not None:
            result["temperature"] = self.temperature
        if self.max_output_tokens is not None:
            result[self.token_limit_field] = self.max_output_tokens
        return result


def default_config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "model-service-kit" / "profiles.json"


class ProfileStore:
    def __init__(
        self, path: Path | None = None, *, credentials: CredentialStore | None = None
    ) -> None:
        self.path = Path(path) if path is not None else default_config_path()
        self.credentials = credentials if credentials is not None else DEFAULT_CREDENTIALS

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 2, "active_profile_id": None, "profiles": []}
        data = _json(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            raise ValueError("模型配置文件格式无效。")
        return data

    def is_legacy(self) -> bool:
        return self._load().get("schema_version") is None

    def _read(self) -> dict:
        data = self._load()
        if "schema_version" not in data:
            raise LegacyConfigError("检测到旧配置，请确认后显式迁移；尚未修改原文件。")
        if data["schema_version"] != 2:
            raise ValueError("模型配置版本不受支持。")
        if set(data) != {"schema_version", "active_profile_id", "profiles"}:
            raise ValueError("新版配置根字段无效，不接受额外字段或明文凭据。")
        ids = set()
        for item in data["profiles"]:
            if not isinstance(item, dict) or "api_key" in item:
                raise ValueError("新版配置不得含明文密钥字段。")
            try:
                profile = ModelProfile(**item)
            except TypeError:
                raise ValueError("模型配置字段无效。") from None
            if profile.profile_id in ids:
                raise ValueError("模型配置标识重复。")
            ids.add(profile.profile_id)
        if data.get("active_profile_id") is not None and data["active_profile_id"] not in ids:
            raise ValueError("活动配置引用不存在。")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=".profiles-", suffix=".json"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def list_profiles(self) -> list[ModelProfile]:
        return [ModelProfile(**item) for item in self._read()["profiles"]]

    def get(self, profile_id: str) -> ModelProfile:
        for profile in self.list_profiles():
            if profile.profile_id == profile_id:
                return profile
        raise KeyError("找不到所选模型配置。")

    def active_profile(self) -> ModelProfile:
        data = self._read()
        for item in data["profiles"]:
            if item["profile_id"] == data.get("active_profile_id"):
                return ModelProfile(**item)
        raise ValueError("尚未选择活动模型配置。")

    def save(
        self,
        profile: ModelProfile,
        *,
        api_key: str | None = None,
        credential_mode: str = "system",
        clear_key: bool = False,
    ) -> ModelProfile:
        data = self._read()
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError("密钥必须是字符串。")
        if clear_key and api_key:
            raise ValueError("不能同时输入新密钥并清除引用。")
        if clear_key:
            profile = replace(profile, credential_ref=None)
        elif not api_key and profile.credential_ref is None:
            previous = next(
                (item for item in data["profiles"] if item["profile_id"] == profile.profile_id),
                None,
            )
            if previous:
                profile = replace(profile, credential_ref=previous.get("credential_ref"))
        new_reference = None
        try:
            if api_key:
                new_reference = self.credentials.put(api_key, mode=credential_mode)
                profile = replace(profile, credential_ref=new_reference)
            profiles = [
                item for item in data["profiles"] if item["profile_id"] != profile.profile_id
            ]
            profiles.append(asdict(profile))
            data["profiles"] = profiles
            self._write(data)
        except Exception:
            if new_reference:
                self.credentials.discard(new_reference)
            raise
        return profile

    def activate(self, profile_id: str) -> None:
        data = self._read()
        if profile_id not in {item["profile_id"] for item in data["profiles"]}:
            raise KeyError("找不到所选模型配置。")
        data["active_profile_id"] = profile_id
        self._write(data)

    def delete(self, profile_id: str) -> None:
        data = self._read()
        if profile_id not in {item["profile_id"] for item in data["profiles"]}:
            raise KeyError("找不到所选模型配置。")
        data["profiles"] = [item for item in data["profiles"] if item["profile_id"] != profile_id]
        if data.get("active_profile_id") == profile_id:
            data["active_profile_id"] = None
        self._write(data)

    def migrate_legacy(self, *, credential_mode: str) -> int:
        data = self._load()
        if data.get("schema_version") is not None:
            raise ValueError("此配置不是待迁移的旧格式。")
        profiles, secrets = [], []
        for item in data["profiles"]:
            if not isinstance(item, dict):
                raise ValueError("旧模型配置格式无效。")
            clean = dict(item)
            secret = clean.pop("api_key", "")
            if not isinstance(secret, str):
                raise ValueError("旧密钥格式无效。")
            try:
                profiles.append(ModelProfile(**clean))
            except TypeError:
                raise ValueError("旧模型配置字段无效。") from None
            secrets.append(secret)
        ids = [profile.profile_id for profile in profiles]
        if len(ids) != len(set(ids)) or (
            data.get("active_profile_id") is not None and data["active_profile_id"] not in ids
        ):
            raise ValueError("旧配置标识或活动引用无效。")
        created = []
        migrated = []
        try:
            for profile, secret in zip(profiles, secrets, strict=True):
                if secret:
                    reference = self.credentials.put(secret, mode=credential_mode)
                    created.append(reference)
                    profile = replace(profile, credential_ref=reference)
                migrated.append(asdict(profile))
            self._write(
                {
                    "schema_version": 2,
                    "active_profile_id": data.get("active_profile_id"),
                    "profiles": migrated,
                }
            )
        except Exception:
            for reference in created:
                self.credentials.discard(reference)
            raise
        return len(migrated)
