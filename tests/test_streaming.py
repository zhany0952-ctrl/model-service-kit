"""通用流式接口自测；受控HTTP／SSE不代表真实模型认知。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from model_service import ChatMessage, ModelProfile, ModelService


def profile(base_url="http://127.0.0.1:12345/v1"):
    return ModelProfile(
        profile_id="test",
        name="本地受控服务",
        base_url=base_url,
        model="controlled",
        api_key="test-secret",
        json_mode=True,
    )


def chunk(text=None, *, finish=None, **delta):
    if text is not None:
        delta["content"] = text
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def sse(*values, done=True):
    text = ": 心跳\r\n\r\n" + "".join(
        "data: " + json.dumps(item, ensure_ascii=False) + "\r\n\r\n" for item in values
    )
    return (text + ("data: [DONE]\r\n\r\n" if done else "")).encode()


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, body, *, width=3):
        self.body, self.width, self.closed = body, width, False

    async def __aiter__(self):
        for index in range(0, len(self.body), self.width):
            yield self.body[index : index + self.width]

    async def aclose(self):
        self.closed = True


def mock_service(body, *, status=200, content_type="text/event-stream"):
    calls = []
    stream = BytesStream(body)

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"content-type": content_type}, stream=stream)

    return ModelService(transport=httpx.MockTransport(handler)), calls, stream


async def collect(service, *, settings=None):
    handle = service.start(
        [ChatMessage("system", "保持原样"), ChatMessage("user", "观察")],
        profile=profile(),
        connect_timeout_s=1,
        read_timeout_s=1,
        **(settings or {}),
    )
    return handle, [event async for event in handle.events()]


def test_stream_request_body_deltas_and_usage():
    async def run():
        usage = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        service, calls, stream = mock_service(
            sse(
                chunk(role="assistant"),
                chunk(reasoning_content="不要作为正文"),
                chunk("你好"),
                chunk("，世界"),
                chunk(finish="stop"),
                {"choices": [], "usage": usage},
            )
        )
        handle, events = await collect(service)
        assert events == [
            {"type": "text_delta", "text": "你好"},
            {"type": "text_delta", "text": "，世界"},
            {"type": "end", "reason": "normal", "raw_reason": "stop", "usage": usage},
        ]
        assert handle.call_id and "test-secret" not in repr(handle)
        assert stream.closed and len(calls) == 1
        body = json.loads(calls[0].content)
        assert body["stream"] is True and "response_format" not in body
        assert body["messages"] == [
            {"role": "system", "content": "保持原样"},
            {"role": "user", "content": "观察"},
        ]
        assert calls[0].headers["authorization"] == "Bearer test-secret"
        assert calls[0].extensions["timeout"]["connect"] == 1
        assert calls[0].extensions["timeout"]["read"] == 1
        await handle.cancel()  # 已终止调用不追加取消结果。

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw, reason", [("stop", "normal"), ("length", "truncated"), ("content_filter", "refused")]
)
def test_end_reason(raw, reason):
    async def run():
        service, _, stream = mock_service(sse(chunk(finish=raw)))
        _, events = await collect(service)
        assert events == [{"type": "end", "reason": reason, "raw_reason": raw}]
        assert stream.closed

    asyncio.run(run())


def test_refusal_is_not_body_or_normal_success():
    async def run():
        service, _, _ = mock_service(sse(chunk(refusal="不能回答"), chunk(finish="stop")))
        _, events = await collect(service)
        assert events == [{"type": "end", "reason": "refused", "raw_reason": "stop"}]

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        b"data: [DONE]\n\n",
        sse(chunk("未结束"), done=False),
        b"data: {broken}\n\n",
        sse({"choices": "bad"}),
        sse(chunk(finish="tool_calls")),
        sse(chunk(finish="unknown")),
        sse(chunk(text=42)),
        sse(chunk(finish="stop"), chunk("终止后的正文")),
        sse({"choices": [{"index": 1, "delta": {"content": "错误分支"}}]}),
        b"data: \xff\n\n",
        b"data: {}",
        sse({"error": {"message": "test-secret"}}),
    ],
)
def test_invalid_or_unfinished_stream_is_protocol_fault(body):
    async def run():
        service, calls, stream = mock_service(body)
        _, events = await collect(service)
        assert events[-1]["type"] == "fault" and events[-1]["code"] == "protocol"
        assert len([e for e in events if e["type"] != "text_delta"]) == 1
        assert "test-secret" not in repr(events)
        assert len(calls) == 1 and stream.closed

    asyncio.run(run())


def test_finish_reason_alone_is_reliable_end_marker():
    async def run():
        service, _, _ = mock_service(sse(chunk("正文"), chunk(finish="stop"), done=False))
        _, events = await collect(service)
        assert events[-1] == {"type": "end", "reason": "normal", "raw_reason": "stop"}

    asyncio.run(run())


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_error_has_status_without_retry_or_secret(status):
    async def run():
        service, calls, stream = mock_service(b"test-secret", status=status)
        _, events = await collect(service)
        assert events == [
            {
                "type": "fault",
                "code": "http",
                "http_status": status,
                "message": f"模型服务返回HTTP {status}。",
            }
        ]
        assert len(calls) == 1 and stream.closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "error, code",
    [
        (httpx.ConnectTimeout("test-secret"), "timeout"),
        (httpx.ReadTimeout("test-secret"), "timeout"),
        (httpx.ConnectError("test-secret"), "network"),
        (httpx.RemoteProtocolError("test-secret"), "protocol"),
    ],
)
def test_transport_errors_are_bounded_and_redacted(error, code):
    async def run():
        def fail(_):
            raise error

        service = ModelService(transport=httpx.MockTransport(fail))
        _, events = await collect(service)
        assert events[-1]["type"] == "fault" and events[-1]["code"] == code
        assert "test-secret" not in repr(events)

    asyncio.run(run())


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "1"])
def test_invalid_timeouts_rejected_before_network(value):
    async def run():
        with pytest.raises(ValueError):
            ModelService().start(
                [ChatMessage("user", "观察")],
                profile=profile(),
                connect_timeout_s=value,
                read_timeout_s=1,
            )
        with pytest.raises(ValueError):
            ModelService().start(
                [ChatMessage("user", "观察")],
                profile=profile(),
                connect_timeout_s=1,
                read_timeout_s=value,
            )

    asyncio.run(run())


def test_immediate_cancel_and_single_consumer():
    async def run():
        service, calls, _ = mock_service(sse(chunk("不应该发出请求"), chunk(finish="stop")))
        handle = service.start(
            [ChatMessage("user", "观察")], profile=profile(), connect_timeout_s=1, read_timeout_s=1
        )
        await handle.cancel()  # 在网络任务首次调度前直接请求取消。
        await asyncio.gather(handle.cancel(), handle.cancel())
        assert [event async for event in handle.events()] == [
            {"type": "end", "reason": "cancelled"}
        ]
        assert not calls
        with pytest.raises(RuntimeError, match="一次"):
            _ = [event async for event in handle.events()]

    asyncio.run(run())


@asynccontextmanager
async def local_server(handler):
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    finally:
        server.close()
        await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def read_request(reader):
    headers = await reader.readuntil(b"\r\n\r\n")
    length = next(
        int(line.split(b":", 1)[1])
        for line in headers.split(b"\r\n")
        if line.lower().startswith(b"content-length:")
    )
    return json.loads(await reader.readexactly(length))


def test_real_http_first_delta_before_completion_and_cancellation_closes_connection():
    async def run():
        release, disconnected = asyncio.Event(), asyncio.Event()

        async def handler(reader, writer):
            request = await read_request(reader)
            assert request["stream"] is True
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            # 跨字节发送中文，正文交付后服务仍等待，不等全文结束才返回。
            for byte in sse(chunk("第一片"), done=False):
                writer.write(bytes([byte]))
            await writer.drain()
            await reader.read()
            disconnected.set()
            await release.wait()

        async with local_server(handler) as address:
            handle = ModelService().start(
                [ChatMessage("user", "观察")],
                profile=profile(address),
                connect_timeout_s=1,
                read_timeout_s=1,
            )
            events = handle.events()
            assert await asyncio.wait_for(anext(events), 2) == {
                "type": "text_delta",
                "text": "第一片",
            }
            await asyncio.wait_for(handle.cancel(), 1)
            assert await anext(events) == {"type": "end", "reason": "cancelled"}
            await asyncio.wait_for(disconnected.wait(), 1)
            release.set()

    asyncio.run(run())


def test_real_http_read_idle_timeout():
    async def run():
        async def handler(reader, writer):
            await read_request(reader)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            await writer.drain()
            await reader.read()

        async with local_server(handler) as address:
            handle = ModelService().start(
                [ChatMessage("user", "观察")],
                profile=profile(address),
                connect_timeout_s=1,
                read_timeout_s=0.05,
            )
            events = await asyncio.wait_for(_all(handle), 2)
            assert events[-1]["type"] == "fault" and events[-1]["code"] == "timeout"

    asyncio.run(run())


async def _all(handle):
    return [event async for event in handle.events()]


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
def test_sse_line_endings_bom_and_multiline_data(separator):
    async def run():
        # event/id/retry不是正文；多行data按SSE规则拼接后再解析JSON。
        body = separator.join(
            [
                "\ufeff: 注释",
                "",
                "event: message",
                "id: 7",
                "retry: 1000",
                'data: {"choices":',
                'data: [{"index":0,"delta":{"content":"中文"},"finish_reason":"stop"}]}',
                "",
                "data: [DONE]",
                "",
                "",
            ]
        ).encode()
        service, _, _ = mock_service(body)
        _, events = await collect(service)
        assert events == [
            {"type": "text_delta", "text": "中文"},
            {"type": "end", "reason": "normal", "raw_reason": "stop"},
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    "usage", ["bad", {"prompt_tokens": -1}, {"total_tokens": True}, {"completion_tokens": 1.5}]
)
def test_malformed_usage_is_not_silently_coerced(usage):
    async def run():
        service, _, _ = mock_service(sse(chunk(finish="stop"), {"choices": [], "usage": usage}))
        _, events = await collect(service)
        assert events[-1]["code"] == "protocol"

    asyncio.run(run())


def test_request_is_frozen_and_call_ids_are_unique():
    async def run():
        service, calls, _ = mock_service(sse(chunk(finish="stop")))
        messages = [ChatMessage("user", "原背景")]
        first = service.start(messages, profile=profile(), connect_timeout_s=1, read_timeout_s=1)
        messages.append(ChatMessage("user", "新增信息不得进入在途请求"))
        await _all(first)
        assert json.loads(calls[0].content)["messages"] == [{"role": "user", "content": "原背景"}]
        second = service.start(messages, profile=profile(), connect_timeout_s=1, read_timeout_s=1)
        assert second.call_id != first.call_id
        await second.cancel()

    asyncio.run(run())


def test_concurrent_cancel_during_blocked_read_closes_once():
    async def run():
        entered = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            closes = 0

            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                self.closes += 1

        stream = WaitingStream()
        service = ModelService(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
            )
        )
        handle = service.start(
            [ChatMessage("user", "观察")], profile=profile(), connect_timeout_s=1, read_timeout_s=1
        )
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(asyncio.gather(handle.cancel(), handle.cancel()), 1)
        assert await _all(handle) == [{"type": "end", "reason": "cancelled"}]
        assert stream.closes == 1

    asyncio.run(run())


def test_cancel_while_waiting_response_headers():
    async def run():
        entered = asyncio.Event()

        async def handler(_):
            entered.set()
            await asyncio.Event().wait()

        service = ModelService(transport=httpx.MockTransport(handler))
        handle = service.start(
            [ChatMessage("user", "观察")], profile=profile(), connect_timeout_s=1, read_timeout_s=1
        )
        await entered.wait()
        await handle.cancel()
        assert await _all(handle) == [{"type": "end", "reason": "cancelled"}]

    asyncio.run(run())


def test_non_sse_response_is_protocol_fault():
    async def run():
        service, _, stream = mock_service(
            b'{"content":"test-secret"}', content_type="application/json"
        )
        _, events = await collect(service)
        assert events[-1]["code"] == "protocol" and stream.closed
        assert "test-secret" not in repr(events)

    asyncio.run(run())


def test_empty_message_list_does_not_open_connection():
    async def run():
        service, calls, _ = mock_service(b"")
        with pytest.raises(ValueError, match="不能为空"):
            service.start([], profile=profile(), connect_timeout_s=1, read_timeout_s=1)
        assert not calls

    asyncio.run(run())
