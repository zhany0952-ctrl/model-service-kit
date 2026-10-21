"""独立连接测试：只使用受控传输，不读取用户配置或调用真实模型。"""

import asyncio
import json
from threading import Event

import httpx
import pytest

from model_service import ModelProfile, ModelService
from model_service.probe import check_connection


def settings():
    return ModelProfile(
        "local",
        "受控配置",
        "http://127.0.0.1:12345/v1",
        "controlled",
        max_output_tokens=800,
        temperature=0.2,
    )


def response(text="连接正常", finish="stop"):
    records = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]
    return "".join("data: " + json.dumps(r) + "\n\n" for r in records) + "data: [DONE]\n\n"


def test_probe_short_independent_request_does_not_mutate_profile():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=response("任意有效正文，不要求逐字匹配"),
        )

    profile = settings()
    result = asyncio.run(
        check_connection(ModelService(transport=httpx.MockTransport(handler)), profile)
    )
    assert result.outcome == "passed" and result.first_text_s is not None
    assert result.elapsed_s >= result.first_text_s and result.text_chars > 0
    assert calls[0]["messages"] == [
        {"role": "user", "content": "这是独立连接测试。请只回复：连接正常。"}
    ]
    assert calls[0]["max_completion_tokens"] == 64
    assert profile.max_output_tokens == 800
    assert "任意有效正文" not in repr(result)  # 不回显服务生成内容，避免内容进入诊断日志。


def test_probe_http_error_is_safe_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="private-secret")

    result = asyncio.run(
        check_connection(ModelService(transport=httpx.MockTransport(handler)), settings())
    )
    assert result.outcome == "failed" and "401" in result.message
    assert len(calls) == 1 and "private-secret" not in repr(result)


def test_probe_truncated_empty_and_refused_are_not_success():
    for text, finish in [("有正文", "length"), ("", "stop"), ("", "content_filter")]:
        service = ModelService(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=response(text, finish),
                )
            )
        )
        assert asyncio.run(check_connection(service, settings())).outcome == "failed"


def test_probe_total_timeout_closes_stream():
    class Hanging(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    stream = Hanging()
    service = ModelService(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    )
    result = asyncio.run(check_connection(service, settings(), total_timeout_s=0.03))
    assert result.outcome == "timeout" and stream.closed


def test_probe_cancel_before_start_does_not_send_request():
    cancelled = Event()
    cancelled.set()

    def forbidden(_):
        raise AssertionError("取消后不得请求模型")

    result = asyncio.run(
        check_connection(
            ModelService(transport=httpx.MockTransport(forbidden)),
            settings(),
            cancel_requested=cancelled,
        )
    )
    assert result.outcome == "cancelled"


def test_probe_cancel_during_read_closes_stream():
    async def run():
        cancelled = Event()

        class Hanging(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                cancelled.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                self.closed = True

        stream = Hanging()
        service = ModelService(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
            )
        )
        result = await check_connection(service, settings(), cancel_requested=cancelled)
        assert result.outcome == "cancelled" and stream.closed

    asyncio.run(run())


def test_probe_progress_distinguishes_reasoning_from_no_response():
    async def run():
        reports = []

        class Thinking(httpx.AsyncByteStream):
            async def __aiter__(self):
                record = {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": "private-thinking-secret"},
                            "finish_reason": None,
                        }
                    ]
                }
                yield ("data: " + json.dumps(record) + "\n\n").encode()
                await asyncio.Event().wait()

        service = ModelService(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=Thinking()
                )
            )
        )
        result = await check_connection(
            service, settings(), total_timeout_s=0.12, on_progress=reports.append
        )
        assert result.outcome == "timeout" and result.text_chars == 0
        info = result.diagnostics
        assert info["http_status"] == 200 and info["reasoning_chars"] > 0
        assert info["received_bytes"] > 0 and info["event_count"] == 1
        assert "private-thinking-secret" not in repr(result) + repr(reports)
        assert any(r.get("reasoning_chars", 0) > 0 for r in reports)
        assert info["last_stage"] == "receiving_reasoning"

    asyncio.run(run())


def test_probe_no_headers_timeout_is_not_reported_as_connected():
    async def handler(_):
        await asyncio.Event().wait()

    service = ModelService(transport=httpx.MockTransport(handler))
    result = asyncio.run(check_connection(service, settings(), total_timeout_s=0.03))
    assert result.diagnostics["last_stage"] == "waiting_headers"
    assert "http_status" not in result.diagnostics
    assert "HTTP响应头" in result.message


def test_probe_diagnostics_http_fault_hint_no_raw_body():
    service = ModelService(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                404, text="private-error-body-and-secret", headers={"x-private-key": "secret"}
            )
        )
    )
    result = asyncio.run(check_connection(service, settings()))
    assert result.diagnostics["http_status"] == 404
    assert "路径或模型" in result.message
    assert "private-error" not in repr(result) and "x-private-key" not in repr(result)


def test_probe_records_finish_reason_and_first_data_milestones():
    service = ModelService(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=response()
            )
        )
    )
    result = asyncio.run(check_connection(service, settings()))
    stages = [item["stage"] for item in result.diagnostics["timeline"]]
    assert stages.index("waiting_headers") < stages.index("response_headers")
    assert stages.index("first_data") < stages.index("first_event")
    assert "first_text" in stages and "finish_reason" in stages and stages[-1] == "closed"
    assert result.diagnostics["raw_reason"] == "stop"


@pytest.mark.parametrize(
    "error, hint",
    [
        (httpx.ConnectTimeout, "建立连接超时"),
        (httpx.ReadTimeout, "读取空闲超时"),
        (httpx.ConnectError, "无法建立连接"),
    ],
)
def test_probe_distinguishes_transport_errors_without_echo(error, hint):
    def handler(_):
        raise error("private-network-secret-and-url")

    result = asyncio.run(
        check_connection(ModelService(transport=httpx.MockTransport(handler)), settings())
    )
    assert result.diagnostics["error_type"] == error.__name__
    assert hint in result.message and "private-network" not in repr(result)


def test_diagnostics_are_opt_in_and_do_not_change_model_events():
    async def run(enabled):
        service = ModelService(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=response()
                )
            )
        )
        from model_service import ChatMessage

        handle = service.start(
            [ChatMessage("user", "测试")], profile=settings(), diagnostics=enabled
        )
        events = [event async for event in handle.events()]
        if enabled:
            snapshot = handle.diagnostics()
            snapshot["timeline"].clear()
            assert handle.diagnostics()["timeline"]
        return handle.diagnostics(), events

    plain, events = asyncio.run(run(False))
    detailed, detailed_events = asyncio.run(run(True))
    assert plain == {} and events == detailed_events
    assert detailed["http_status"] == 200


def test_probe_real_loopback_progress_then_timeout_releases_socket():
    async def run():
        disconnected = asyncio.Event()
        reports = []

        async def handler(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
                record = {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": "private-live-reasoning"},
                            "finish_reason": None,
                        }
                    ]
                }
                writer.write(("data: " + json.dumps(record) + "\n\n").encode())
                await writer.drain()
                await reader.read()
            finally:
                disconnected.set()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        profile = ModelProfile("tcp", "受控TCP", f"http://127.0.0.1:{port}/v1", "controlled")
        try:
            result = await check_connection(
                ModelService(), profile, total_timeout_s=0.35, on_progress=reports.append
            )
            await asyncio.wait_for(disconnected.wait(), 2)
        finally:
            server.close()
            await server.wait_closed()
        assert result.outcome == "timeout" and result.text_chars == 0
        assert any(r.get("reasoning_chars", 0) > 0 and r.get("stage") != "closed" for r in reports)
        assert "private-live-reasoning" not in repr(reports) + repr(result)

    asyncio.run(run())


def test_probe_opt_in_preview_separates_content_without_logging_it():
    records = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "private-thought"},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"content": "private-answer"}, "finish_reason": "stop"}
            ]
        },
        {"choices": [], "usage": {"total_tokens": 9}},
    ]
    body = "".join("data: " + json.dumps(r) + "\n\n" for r in records) + "data: [DONE]\n\n"
    service = ModelService(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        )
    )
    previews, progress = [], []

    async def run():
        async with service:
            plain = await check_connection(service, settings())
            assert plain.preview is None
            return await check_connection(
                service,
                settings(),
                capture_content=True,
                on_content=previews.append,
                on_progress=progress.append,
            )

    detailed = asyncio.run(run())
    assert detailed.preview["text"] == "private-answer"
    assert detailed.preview["reasoning"] == "private-thought"
    assert "private-" not in repr(detailed) + repr(progress)
    assert previews[-1] == detailed.preview
    assert detailed.diagnostics["last_stage"] == "finish_reason"  # 尾部用量不覆盖阶段。


def test_preview_bounds_and_masks_credentials_split_across_chunks():
    async def run():
        from model_service import ChatMessage, CredentialStore

        credentials = CredentialStore()
        secret = "private-api-key"
        reference = credentials.put(secret, mode="session")
        profile = ModelProfile(
            "test", "受控", "http://127.0.0.1:12345/v1", "controlled", credential_ref=reference
        )

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield response("private-api-", finish=None).replace("data: [DONE]\n\n", "").encode()
                await asyncio.sleep(0.02)
                yield response("key" + "字" * 5000).encode()

        service = ModelService(
            credentials=credentials,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=Stream()
                )
            ),
        )
        handle = service.start(
            [ChatMessage("user", "测试")], profile=profile, capture_content=True, diagnostics=True
        )
        async for _ in handle.events():
            preview = handle.preview()
            assert secret not in preview["text"] and "private-api-" not in preview["text"]
        assert handle.preview()["truncated"] and len(handle.preview()["text"]) < 4200
        assert "private-api-" not in repr(handle.diagnostics())

    asyncio.run(run())
