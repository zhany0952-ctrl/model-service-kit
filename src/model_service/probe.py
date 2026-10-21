"""独立短消息测试；默认不采集内容，可选有限内存预览，不携带业务上下文。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import perf_counter

from .client import ChatMessage, ModelService
from .config import ModelProfile

PROBE_TIMEOUT_SECONDS = 30.0
PROBE_MAX_TOKENS = 64
PROBE_MESSAGE = "这是独立连接测试。请只回复：连接正常。"


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    message: str
    elapsed_s: float
    first_text_s: float | None
    text_chars: int
    diagnostics: dict = field(default_factory=dict)
    preview: dict | None = field(default=None, repr=False)


def diagnostic_hint(info):
    """只依据客户端证据给排查建议，不猜测模型内部加载／排队状态。"""
    status = info.get("http_status")
    if info.get("error_type") == "CredentialError":
        return "凭据读取失败，请重新配置密钥或保存方式；尚未发送模型HTTP请求。"
    if info.get("error_type") in {"ProtocolFault", "InvalidEvent"}:
        return "响应不符合当前纯文本流式协议，请核对服务兼容性；不回显原始响应内容。"
    if status is not None and not 200 <= status < 300:
        return {
            400: "请求参数或协议可能不受支持，请核对服务文档。",
            401: "认证未通过，请核对密钥与认证方式。",
            403: "服务拒绝访问，请核对模型权限。",
            404: "路径或模型未找到，请核对根地址和模型名称。",
            429: "请求受限，请检查额度或服务并发限制。",
        }.get(status, "服务返回失败状态，请结合服务端日志排查。")
    if info.get("error_type") == "ConnectTimeout":
        return "建立连接超时，请检查服务是否启动、地址及网络。"
    if info.get("error_type") == "ConnectError":
        return "无法建立连接，请检查服务启动状态、端口和网络。"
    if info.get("error_type") == "ReadTimeout":
        return "读取空闲超时；期间没有新的网络数据，不等于服务端停止计算。"
    if status is None:
        return "尚未收到HTTP响应头；客户端无法确认服务端是否正在加载或排队，请查服务端日志。"
    if info.get("reasoning_chars", 0) and not info.get("text_chars", 0):
        return "已收到独立思考字段，但尚无正文；不是完全无响应。可检查思考设置及测试预算。"
    if not info.get("received_bytes", 0):
        return "已收到HTTP响应头，但尚无响应体数据；内部原因仍需服务端日志确认。"
    if not info.get("event_count", 0):
        return "已收到响应体数据，但未形成有效协议事件，请检查流式格式。"
    if info.get("raw_reason") == "length":
        return "输出预算已耗尽；本次测试至多64 token，不等于模型不可用。"
    return "结合已观测阶段和服务端日志排查；本工具不读取模型内部状态。"


async def check_connection(
    service: ModelService,
    profile: ModelProfile,
    *,
    cancel_requested: Event | None = None,
    total_timeout_s: float = PROBE_TIMEOUT_SECONDS,
    on_progress=None,
    capture_content: bool = False,
    on_content=None,
) -> ProbeResult:
    """测试配置快照；限制只作用于本次请求，不改变保存配置或活动模型。"""
    started = perf_counter()
    first_text, chars, has_text = None, 0, False
    handle = None

    def report():
        info = handle.diagnostics() if handle is not None else {}
        if on_progress is not None:
            on_progress(info)
        if capture_content and on_content is not None and handle is not None:
            on_content(handle.preview())
        return info

    def result(outcome, message):
        info = report()
        if outcome in {"timeout", "failed"}:
            message += " " + diagnostic_hint(info)
        preview = handle.preview() if capture_content and handle is not None else None
        return ProbeResult(
            outcome, message, perf_counter() - started, first_text, chars, info, preview
        )

    if cancel_requested is not None and cancel_requested.is_set():
        return result("cancelled", "已取消，未发送请求。")
    probe_profile = replace(
        profile,
        max_output_tokens=min(profile.max_output_tokens or PROBE_MAX_TOKENS, PROBE_MAX_TOKENS),
    )
    handle = service.start(
        [ChatMessage("user", PROBE_MESSAGE)],
        profile=probe_profile,
        diagnostics=True,
        capture_content=capture_content,
    )
    report()

    async def receive():
        nonlocal first_text, chars, has_text
        async for event in handle.events():
            if event["type"] == "text_delta":
                if first_text is None:
                    first_text = perf_counter() - started
                chars += len(event["text"])
                has_text = has_text or bool(event["text"].strip())
            elif event["type"] == "fault":
                status = event.get("http_status")
                suffix = f"（HTTP {status}）" if status is not None else ""
                return result("failed", event["message"] + suffix)
            elif event["reason"] == "normal" and has_text:
                return result("passed", "收到有效正文且正常结束；仅证明当前调用链可用。")
            else:
                descriptions = {
                    "normal": "正常结束但没有有效正文。",
                    "truncated": "链路有响应，但输出被截断；未通过完整调用检查。",
                    "refused": "服务明确拒绝；未通过完整调用检查。",
                    "cancelled": "本地接收已取消，不保证服务端已停止生成。",
                }
                reason = event["reason"]
                return result(
                    "cancelled" if reason == "cancelled" else "failed", descriptions[reason]
                )
        return result("failed", "调用未提供可靠终止结果。")

    async def wait_cancel():
        while cancel_requested is None or not cancel_requested.is_set():
            report()
            await asyncio.sleep(0.05)

    consumer = asyncio.create_task(receive())
    stopper = asyncio.create_task(wait_cancel())
    try:
        done, _ = await asyncio.wait(
            (consumer, stopper), timeout=total_timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if consumer in done:
            return consumer.result()
        await handle.cancel()
        if stopper in done:
            return result("cancelled", "本地接收已取消，不保证服务端已停止生成。")
        return result("timeout", "独立测试超过总时限，已停止本地接收。")
    finally:
        await handle.cancel()
        for task in (consumer, stopper):
            task.cancel()
        await asyncio.gather(consumer, stopper, return_exceptions=True)


class ProbeJob:
    """后台线程只处理网络和队列；Tk控件仅由主线程操作。"""

    def __init__(self, service: ModelService, profile: ModelProfile, *, capture_content=False):
        self.cancel_requested = Event()
        self.results: Queue[ProbeResult] = Queue()
        self.progress: Queue[dict] = Queue(maxsize=1)
        self.contents: Queue[dict] = Queue(maxsize=1)
        self.capture_content = capture_content
        self.thread = Thread(target=self._run, args=(service, profile), daemon=True)

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancel_requested.set()

    def _publish(self, info):
        self._latest(self.progress, info)

    def _publish_content(self, preview):
        self._latest(self.contents, preview)

    @staticmethod
    def _latest(queue, value):
        # 只保留最新累计快照；里程碑时间线随快照携带，不因UI较慢无限排队。
        try:
            queue.put_nowait(value)
        except Full:
            try:
                queue.get_nowait()
            except Empty:
                pass
            queue.put_nowait(value)

    def _run(self, service, profile):
        started = perf_counter()

        async def run():
            async with service:
                return await check_connection(
                    service,
                    profile,
                    cancel_requested=self.cancel_requested,
                    on_progress=self._publish,
                    capture_content=self.capture_content,
                    on_content=self._publish_content,
                )

        try:
            result = asyncio.run(run())
        except Exception:
            # 不输出底层异常、密钥、地址或模型生成正文。
            result = ProbeResult(
                "failed", "连接测试未能正常执行。", perf_counter() - started, None, 0
            )
        self.results.put(result)
