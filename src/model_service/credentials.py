"""系统凭据与仅进程凭据；配置文件只持有不可推测的引用。"""

from __future__ import annotations

import re
from threading import RLock
from uuid import uuid4

SERVICE = "model-service-kit"
REFERENCE = re.compile(r"(?:keyring|session):[0-9a-f]{32}\Z")


class CredentialError(RuntimeError):
    """安全的直接错误，不转发后端异常正文。"""


class CredentialStore:
    def __init__(self, *, backend=None) -> None:
        self._backend_override = backend
        self._session: dict[str, str] = {}
        self._lock = RLock()

    def __repr__(self) -> str:
        return "CredentialStore()"

    def _backend(self):
        if self._backend_override is not None:
            return self._backend_override  # 显式注入用于测试，不自动选择第三方文件后端。
        try:
            import keyring

            backend = keyring.get_keyring()
            if type(backend).__module__ not in {
                "keyring.backends.Windows",
                "keyring.backends.macOS",
                "keyring.backends.SecretService",
                "keyring.backends.kwallet",
            }:
                raise CredentialError("系统凭据库不可用，请显式选择仅本次进程。")
            return backend
        except Exception:
            raise CredentialError("系统凭据库不可用，请显式选择仅本次进程。") from None

    def put(self, secret: str, *, mode: str = "system") -> str:
        if not isinstance(secret, str) or not secret.strip() or any(c in secret for c in "\r\n\0"):
            raise ValueError("密钥不能为空或包含换行控制字符。")
        if mode not in {"system", "session"}:
            raise ValueError("凭据保存方式必须为系统凭据库或仅本次进程。")
        reference = ("keyring:" if mode == "system" else "session:") + uuid4().hex
        if mode == "session":
            with self._lock:
                self._session[reference] = secret
        else:
            try:
                self._backend().set_password(SERVICE, reference, secret)
            except Exception:
                raise CredentialError("系统凭据库保存失败，未降级为明文或进程保存。") from None
        return reference

    def get(self, reference: str | None) -> str:
        if reference is None:
            return ""
        if not isinstance(reference, str) or not REFERENCE.fullmatch(reference):
            raise CredentialError("凭据引用无效，请重新输入密钥。")
        if reference.startswith("session:"):
            with self._lock:
                secret = self._session.get(reference)
        else:
            try:
                secret = self._backend().get_password(SERVICE, reference)
            except Exception:
                raise CredentialError(
                    "系统凭据库读取失败，请检查保存方式或重新输入密钥。"
                ) from None
        if not isinstance(secret, str) or not secret:
            raise CredentialError("凭据不存在或已离开原进程，请重新输入密钥。")
        return secret

    def discard(self, reference: str) -> None:
        """只用于撤销本次保存失败所新建的引用，不清理历史或用户已有密钥。"""
        if not REFERENCE.fullmatch(reference):
            raise CredentialError("凭据引用无效。")
        if reference.startswith("session:"):
            with self._lock:
                self._session.pop(reference, None)
        else:
            try:
                self._backend().delete_password(SERVICE, reference)
            except Exception:
                raise CredentialError("新建凭据未能撤销；普通配置仍未写入密钥。") from None


DEFAULT_CREDENTIALS = CredentialStore()
