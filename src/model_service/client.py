"""通用模型服务入口；保留旧同步接口，新增独立的异步流式调用。"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx

from .config import ModelProfile, ProfileStore
from .credentials import CredentialError, CredentialStore

if TYPE_CHECKING:
    from .streaming import StreamHandle


class ModelServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"} or not self.content:
            raise ValueError("无效的聊天消息")


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    response_id: str


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


class ModelService:
    def __init__(
        self,
        store: ProfileStore | None = None,
        *,
        transport=None,
        credentials: CredentialStore | None = None,
        keepalive_expiry_s: float = 5.0,
    ) -> None:
        if (
            type(keepalive_expiry_s) not in {int, float}
            or not math.isfinite(keepalive_expiry_s)
            or keepalive_expiry_s <= 0
        ):
            raise ValueError("连接空闲保留时间须为正的有限秒数。")
        self.store = store or ProfileStore()
        self.credentials = credentials if credentials is not None else self.store.credentials
        self._transport = transport
        self._client = None
        self._loop = None
        self._handles = set()
        self._closed = False
        self._close_task = None
        self._keepalive_expiry_s = keepalive_expiry_s

    def _bind_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("同一模型服务须在同一事件循环使用并关闭。")
        self._loop = loop

    def _http_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                cookies=CookieJar(policy=_NoCookies()),
                limits=httpx.Limits(keepalive_expiry=self._keepalive_expiry_s),
            )
        return self._client

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError("模型服务已关闭。")
        self._bind_loop()
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        """关闭服务时停止未结束调用，最后释放池；单次cancel不关闭池。"""
        self._bind_loop()
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)

    async def _shutdown(self):
        try:
            await asyncio.gather(*(handle.cancel() for handle in tuple(self._handles)))
        finally:
            if self._client is not None:
                await self._client.aclose()

    def active_profile(self) -> ModelProfile:
        return self.store.active_profile()

    def start(
        self,
        messages: list[ChatMessage],
        *,
        profile: ModelProfile,
        connect_timeout_s: float | None = None,
        read_timeout_s: float | None = None,
        diagnostics: bool = False,
        capture_content: bool = False,
        capture_timing: bool = False,
    ) -> StreamHandle:
        """在当前异步事件循环发起一次流式调用，不等待网络返回。"""
        from .streaming import StreamHandle

        if self._closed:
            raise RuntimeError("模型服务已关闭。")
        self._bind_loop()
        handle = StreamHandle(
            messages,
            profile=profile,
            connect_timeout_s=(
                profile.connect_timeout_seconds if connect_timeout_s is None else connect_timeout_s
            ),
            read_timeout_s=(
                profile.read_timeout_seconds if read_timeout_s is None else read_timeout_s
            ),
            transport=self._transport,
            credentials=self.credentials,
            diagnostics=diagnostics,
            capture_content=capture_content,
            capture_timing=capture_timing,
            client_provider=self._http_client,
        )
        self._handles.add(handle)
        handle._task.add_done_callback(lambda _: self._handles.discard(handle))
        return handle

    def generate(self, messages: list[ChatMessage], *, profile: ModelProfile) -> ChatResult:
        if not messages:
            raise ValueError("messages 不能为空")
        url = profile.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": profile.model,
            "messages": [{"role": item.role, "content": item.content} for item in messages],
            "stream": False,
        }
        body.update(profile.generation_parameters())
        if profile.json_mode:
            body["response_format"] = {"type": "json_object"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            secret = self.credentials.get(profile.credential_ref)
        except CredentialError:
            raise ModelServiceError("credentials_unavailable") from None
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        request = Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=profile.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ModelServiceError(f"http_{exc.code}") from None
        except (URLError, TimeoutError):
            raise ModelServiceError("connection_or_timeout") from None
        except (ValueError, UnicodeError):
            raise ModelServiceError("invalid_response") from None
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            return ChatResult(
                content=content,
                model=str(data.get("model") or profile.model),
                response_id=str(data.get("id") or "unidentified"),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelServiceError("invalid_response") from None
