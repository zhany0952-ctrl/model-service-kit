"""通用Chat Completions流：只提供正文，不识别NPC区域或调用方业务。"""

from __future__ import annotations

import asyncio
import codecs
import copy
import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from time import perf_counter
from uuid import uuid4

import httpx

from .config import ModelProfile
from .credentials import DEFAULT_CREDENTIALS, CredentialError

_TRACE_STAGES = {
    "connection.connect_tcp.started": ("tcp_started", "tcp_connects"),
    "connection.connect_tcp.complete": ("tcp_complete", None),
    "connection.connect_tcp.failed": ("tcp_failed", None),
    "connection.start_tls.started": ("tls_started", "tls_handshakes"),
    "connection.start_tls.complete": ("tls_complete", None),
    "connection.start_tls.failed": ("tls_failed", None),
    **{
        protocol + "." + source: (stage, counter)
        for protocol in ("http11", "http2")
        for source, stage, counter in (
            ("send_request_headers.started", "request_headers_started", "requests_started"),
            ("send_request_headers.complete", "request_headers_sent", None),
            ("send_request_body.complete", "request_body_sent", None),
            ("receive_response_headers.started", "response_headers_wait", None),
            ("receive_response_headers.complete", "response_headers_received", None),
        )
    },
}


class _ProtocolFault(ValueError):
    """仅承载程序自定义的固定安全说明，不复制服务原文。"""


def _positive_seconds(value) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("连接和读取超时必须是正的有限秒数。")
    return float(value)


async def _lines(response: httpx.Response, on_bytes=None) -> AsyncIterator[str]:
    """严格UTF-8及跨网络分片换行；兼容LF、CRLF和CR。"""
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    pending = ""
    async for block in response.aiter_bytes():
        if on_bytes is not None:
            on_bytes(len(block))
        pending += decoder.decode(block)
        start = 0
        for match in re.finditer(r"\r\n|\r|\n", pending):
            if match.group() == "\r" and match.end() == len(pending):
                break  # 下个分片可能从LF开始，不能把一个CRLF拆成两个空行。
            yield pending[start : match.start()]
            start = match.end()
        pending = pending[start:]
    pending += decoder.decode(b"", final=True)
    if pending:
        parts = re.split(r"\r\n|\r|\n", pending)
        for line in parts[:-1]:
            yield line
        if parts[-1]:
            yield parts[-1]


async def _sse_data(response: httpx.Response, on_bytes=None) -> AsyncIterator[str]:
    data = []
    async for line in _lines(response, on_bytes):
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
    if data:
        raise _ProtocolFault("模型事件未以完整SSE边界结束。")


def _usage(data) -> dict | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise _ProtocolFault("模型用量结构无效。")
    result = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if name in data:
            value = data[name]
            if type(value) is not int or value < 0:
                raise _ProtocolFault("模型用量必须为非负整数。")
            result[name] = value
    return result or None


class StreamHandle:
    """一个句柄对应一次请求、一个事件消费者与一个终止结果。"""

    def __init__(
        self,
        messages,
        *,
        profile: ModelProfile,
        connect_timeout_s: float,
        read_timeout_s: float,
        transport=None,
        credentials=None,
        diagnostics=False,
        capture_content=False,
        capture_timing=False,
        client_provider=None,
    ) -> None:
        connect = _positive_seconds(connect_timeout_s)
        read = _positive_seconds(read_timeout_s)
        if not messages or any(
            not isinstance(item.role, str)
            or not item.role.strip()
            or not isinstance(item.content, str)
            or not item.content.strip()
            for item in messages
        ):
            raise ValueError("消息列表及每条消息的角色、正文不能为空。")
        # 在返回句柄前复制调用输入，不允许调用方之后修改列表改变在途请求。
        body = {
            "model": profile.model,
            "messages": [{"role": item.role, "content": item.content} for item in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        body.update(profile.generation_parameters())
        headers = {"Accept": "text/event-stream"}
        self._credentials = credentials if credentials is not None else DEFAULT_CREDENTIALS
        self._client_provider = client_provider
        timeout = httpx.Timeout(connect=connect, read=read, write=connect, pool=connect)
        self.call_id = "call-" + uuid4().hex
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._terminal = None
        self._consumed = self._cancel_requested = False
        self._started = perf_counter()
        self._text_timing = [] if capture_timing else None
        self._text_position = 0
        self._preview = (
            {"text": "", "reasoning": "", "truncated": False} if capture_content else None
        )
        self._preview_secret = ""
        self._diagnostic_state = (
            {
                "stage": "prepared",
                "received_bytes": 0,
                "network_chunks": 0,
                "event_count": 0,
                "text_chars": 0,
                "text_chunks": 0,
                "reasoning_chars": 0,
                "reasoning_chunks": 0,
                "started_monotonic_s": self._started,
                "transport_stats": {
                    "trace_events": 0, "tcp_connects": 0,
                    "tls_handshakes": 0, "requests_started": 0,
                },
                "timeline": [],
            }
            if diagnostics
            else None
        )
        self._task = asyncio.get_running_loop().create_task(
            self._run(
                profile.base_url.rstrip("/") + "/chat/completions",
                body,
                headers,
                timeout,
                transport,
                profile.credential_ref,
            )
        )
        self._task.add_done_callback(self._task_finished)

    def terminal(self) -> dict | None:
        """非阻塞读取终态副本；未终止返回None，不消费或重放事件流。"""
        return copy.deepcopy(self._terminal)

    def text_timing(self) -> list[dict] | None:
        """可选正文SSE增量的本地接收时刻和字符区间；不包含正文或独立思考。"""
        return copy.deepcopy(self._text_timing)

    def preview(self) -> dict | None:
        """显式选择的有限内容预览，与数值诊断分离，不进入模型事件或日志。"""
        if self._preview is None:
            return None

        def masked(text):
            secret = self._preview_secret
            if not secret:
                return text
            text = text.replace(secret, "[密钥已隐藏]")
            # 分片未收齐密钥时也隐藏尾部前缀，避免在实时预览里逐片泄露。
            for length in range(min(len(secret) - 1, len(text)), 0, -1):
                if text.endswith(secret[:length]):
                    return text[:-length] + "[密钥片段已隐藏]"
            return text

        return {
            "text": masked(self._preview["text"]),
            "reasoning": masked(self._preview["reasoning"]),
            "truncated": self._preview["truncated"],
        }

    def _capture_delta(self, delta):
        if self._preview is None:
            return
        for name, value in (
            ("text", delta.get("content")),
            ("reasoning", delta.get("reasoning_content") or delta.get("reasoning")),
        ):
            if isinstance(value, str) and value:
                remaining = 4096 - len(self._preview[name])
                self._preview[name] += value[:remaining]
                if len(value) > remaining:
                    self._preview["truncated"] = True

    def diagnostics(self) -> dict:
        """可选只读诊断快照；不含请求、正文、思考原文、地址、密钥或响应头原文。"""
        if self._diagnostic_state is None:
            return {}
        result = copy.deepcopy(self._diagnostic_state)
        if "elapsed_s" not in result:
            result["elapsed_s"] = perf_counter() - self._started
        return result

    async def _trace(self, event_name, info):
        """仅记录白名单阶段与计数；绝不复制可能含地址／认证的trace info。"""
        if event_name not in _TRACE_STAGES or self._diagnostic_state is None:
            return
        stage, counter = _TRACE_STAGES[event_name]
        stats = self._diagnostic_state["transport_stats"]
        stats["trace_events"] += 1
        if counter is not None:
            stats[counter] += 1
        self._mark(stage)

    def _record_cache_usage(self, usage):
        """保留服务实际返回的可选缓存量；缺失不补零，坏诊断不改变业务终态。"""
        if self._diagnostic_state is None or not isinstance(usage, dict):
            return
        info = self._diagnostic_state

        def warning(code):
            warnings = info.setdefault("diagnostic_warnings", [])
            if code not in warnings:
                warnings.append(code)

        fields = {
            key: usage[key] for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            if key in usage
        }
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and "cached_tokens" in details:
            fields["cached_tokens"] = details["cached_tokens"]
        elif details is not None and not isinstance(details, dict):
            warning("invalid_cache_usage")
        for key, value in fields.items():
            if type(value) is int and value >= 0:
                info.setdefault("cache_usage", {})[key] = value
            else:
                warning("invalid_cache_usage")
        cache = info.get("cache_usage", {})
        if ("cached_tokens" in cache and "prompt_cache_hit_tokens" in cache
                and cache["cached_tokens"] != cache["prompt_cache_hit_tokens"]):
            warning("conflicting_cache_usage")

    def _mark(self, stage, **details):
        info = self._diagnostic_state
        if info is None:
            return
        info.update(details)
        info["stage"] = stage
        # 每类里程碑只记一次，连续分片仅更新累计统计，不存无限日志。
        if not any(item["stage"] == stage for item in info["timeline"]):
            info["timeline"].append({"stage": stage, "elapsed_s": perf_counter() - self._started})

    def _record_bytes(self, count):
        info = self._diagnostic_state
        info["received_bytes"] += count
        info["network_chunks"] += 1
        if not any(item["stage"] == "first_data" for item in info["timeline"]):
            self._mark("first_data")

    def _record_delta(self, delta):
        info = self._diagnostic_state
        if info is None:
            return
        # 只统计服务明确分离的思考字符串，不解析正文中的自定义标签。
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            info["reasoning_chars"] += len(reasoning)
            info["reasoning_chunks"] += 1
            self._mark("first_reasoning")
            self._mark("receiving_reasoning")
        text = delta.get("content")
        if isinstance(text, str) and text:
            info["text_chars"] += len(text)
            info["text_chunks"] += 1
            self._mark("first_text")
            self._mark("receiving_text")

    def _finish(self, terminal: dict) -> None:
        if self._terminal is None:
            if self._diagnostic_state is not None:
                last_stage = self._diagnostic_state["stage"]
                self._mark(
                    "closed",
                    last_stage=last_stage,
                    terminal_kind=terminal["type"],
                    error_code=terminal.get("code"),
                    reason=terminal.get("reason"),
                )
                self._diagnostic_state["elapsed_s"] = perf_counter() - self._started
            self._terminal = terminal
            self._queue.put_nowait(terminal)

    def _task_finished(self, task) -> None:
        # task在首次调度之前就被取消时，协程内部的异常处理尚未运行。
        if task.cancelled():
            self._finish({"type": "end", "reason": "cancelled"})
        elif task.exception() is not None:
            self._finish({"type": "fault", "code": "protocol", "message": "模型流处理未正常结束。"})

    async def events(self) -> AsyncIterator[dict]:
        if self._consumed:
            raise RuntimeError("每个调用只能消费一次事件流。")
        self._consumed = True
        try:
            while True:
                event = await self._queue.get()
                yield event
                if event["type"] != "text_delta":
                    return
        finally:
            if self._terminal is None:
                await self.cancel()

    async def cancel(self) -> None:
        if self._terminal is not None:
            return
        if not self._cancel_requested:
            self._cancel_requested = True
            self._task.cancel()
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise
        if self._task.cancelled():
            self._finish({"type": "end", "reason": "cancelled"})

    async def _run(self, url, body, headers, timeout, transport, credential_ref) -> None:
        try:
            if credential_ref:
                self._mark("credential_lookup")
                secret = await asyncio.to_thread(self._credentials.get, credential_ref)
                if self._preview is not None:
                    self._preview_secret = secret
                headers["Authorization"] = f"Bearer {secret}"
            # 不跟随重定向转发认证、不自动重试，不从环境读取代理或其他凭据。
            async with AsyncExitStack() as scope:
                client = (
                    self._client_provider()
                    if self._client_provider is not None
                    else await scope.enter_async_context(
                        httpx.AsyncClient(
                            timeout=timeout,
                            transport=transport,
                            follow_redirects=False,
                            trust_env=False,
                        )
                    )
                )
                self._mark("waiting_headers")
                async with client.stream(
                    "POST", url, json=body, headers=headers, timeout=timeout,
                    **({"extensions": {"trace": self._trace}}
                       if self._diagnostic_state is not None else {}),
                ) as response:
                    content_type = response.headers.get("content-type", "").split(";")[0].lower()
                    safe_type = (
                        content_type
                        if content_type
                        in {"text/event-stream", "application/json", "text/html", "text/plain"}
                        else "其他或缺失"
                    )
                    self._mark(
                        "response_headers", http_status=response.status_code, content_type=safe_type
                    )
                    if not 200 <= response.status_code < 300:
                        terminal = {
                            "type": "fault",
                            "code": "http",
                            "http_status": response.status_code,
                            "message": f"模型服务返回HTTP {response.status_code}。",
                        }
                    elif content_type != "text/event-stream":
                        raise _ProtocolFault("模型服务未返回SSE事件流。")
                    else:
                        terminal = await self._receive(response)
        except asyncio.CancelledError:
            terminal = {"type": "end", "reason": "cancelled"}
        except CredentialError:
            if self._diagnostic_state is not None:
                self._diagnostic_state["error_type"] = "CredentialError"
            terminal = {
                "type": "fault",
                "code": "protocol",
                "message": "模型凭据不可用，请重新配置。",
            }
        except httpx.TimeoutException as exc:
            if self._diagnostic_state is not None:
                self._diagnostic_state["error_type"] = type(exc).__name__
            terminal = {"type": "fault", "code": "timeout", "message": "模型服务连接或读取超时。"}
        except httpx.ProtocolError as exc:
            if self._diagnostic_state is not None:
                self._diagnostic_state["error_type"] = type(exc).__name__
            terminal = {"type": "fault", "code": "protocol", "message": "模型服务HTTP传输不完整。"}
        except httpx.RequestError as exc:
            if self._diagnostic_state is not None:
                self._diagnostic_state["error_type"] = type(exc).__name__
            terminal = {"type": "fault", "code": "network", "message": "模型服务网络连接失败。"}
        except _ProtocolFault as exc:
            if self._diagnostic_state is not None:
                self._diagnostic_state["error_type"] = "ProtocolFault"
            terminal = {"type": "fault", "code": "protocol", "message": str(exc)}
        except (ValueError, UnicodeError, TypeError):
            if self._diagnostic_state is not None:
                self._diagnostic_state["error_type"] = "InvalidEvent"
            terminal = {"type": "fault", "code": "protocol", "message": "模型服务事件格式无效。"}
        # 先释放本次响应，再公布唯一终止事件；正常连接可回池，取消不影响其他调用。
        self._finish(terminal)

    async def _receive(self, response) -> dict:
        raw_reason, usage, refused = None, None, False
        done = False
        on_bytes = self._record_bytes if self._diagnostic_state is not None else None
        async for payload in _sse_data(response, on_bytes):
            if done:
                raise _ProtocolFault("DONE后仍返回模型事件。")
            if payload == "[DONE]":
                # 读到HTTP响应体结束才可安全回池，提前退出会丢弃本次TCP连接。
                done = True
                continue
            record = json.loads(payload)
            if not isinstance(record, dict) or "error" in record:
                raise _ProtocolFault("模型服务返回无效事件或服务错误。")
            if self._diagnostic_state is not None:
                self._diagnostic_state["event_count"] += 1
                if self._diagnostic_state["event_count"] == 1:
                    self._mark("first_event")
            current_usage = _usage(record.get("usage"))
            self._record_cache_usage(record.get("usage"))
            if current_usage is not None:
                usage = current_usage
            choices = record.get("choices")
            if not isinstance(choices, list):
                raise _ProtocolFault("模型事件缺少有效choices。")
            if not choices:
                if current_usage is None:
                    raise _ProtocolFault("空choices未携带用量。")
                continue
            if len(choices) != 1 or not isinstance(choices[0], dict):
                raise _ProtocolFault("只支持单个文本生成分支。")
            choice = choices[0]
            if type(choice.get("index")) is not int or choice["index"] != 0:
                raise _ProtocolFault("模型生成分支标识无效。")
            if raw_reason is not None:
                raise _ProtocolFault("服务结束标记后仍返回生成分支。")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise _ProtocolFault("模型正文增量结构无效。")
            if self._diagnostic_state is not None:
                self._record_delta(delta)
            if self._preview is not None:
                self._capture_delta(delta)
            if delta.get("tool_calls") or delta.get("function_call"):
                raise _ProtocolFault("当前文本接口不支持工具调用。")
            refusal = delta.get("refusal")
            if refusal is not None and not isinstance(refusal, str):
                raise _ProtocolFault("服务拒绝字段格式无效。")
            refused = refused or bool(refusal)
            text = delta.get("content")
            if text is not None and not isinstance(text, str):
                raise _ProtocolFault("模型正文增量必须为字符串。")
            if text:
                if self._text_timing is not None:
                    self._text_timing.append(
                        {
                            "start": self._text_position,
                            "end": self._text_position + len(text),
                            "received_monotonic_s": perf_counter(),
                        }
                    )
                    self._text_position += len(text)
                self._queue.put_nowait({"type": "text_delta", "text": text})
            reason = choice.get("finish_reason")
            if reason is not None:
                if reason not in {"stop", "length", "content_filter"}:
                    raise _ProtocolFault("模型服务结束原因不受当前文本接口支持。")
                raw_reason = reason
                self._mark("finish_reason", raw_reason=reason)
        if raw_reason is None:
            raise _ProtocolFault("模型连接结束但没有可靠的结束原因。")
        reason = (
            "refused"
            if refused
            else {
                "stop": "normal",
                "length": "truncated",
                "content_filter": "refused",
            }[raw_reason]
        )
        terminal = {"type": "end", "reason": reason, "raw_reason": raw_reason}
        if usage is not None:
            terminal["usage"] = usage
        return terminal
