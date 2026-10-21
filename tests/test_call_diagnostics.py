"""可选缓存／连接诊断；缺失不补零，不记录trace详情，不改变正文事件。"""

import asyncio
from dataclasses import replace

import httpx
import pytest
from test_streaming import chunk, local_server, profile, read_request, sse

from model_service import ChatMessage, ModelService


async def invoke(service, *, diagnostics=True, settings=None):
    handle = service.start(
        [ChatMessage("user", "相同上下文")], profile=settings or profile(),
        diagnostics=diagnostics,
    )
    events = [e async for e in handle.events()]
    return handle, events


@pytest.mark.parametrize("usage, expected, warnings", [
    ({"prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36,
      "prompt_tokens_details": {"cached_tokens": 64, "private-field": "secret"}},
     {"prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36, "cached_tokens": 64}, []),
    ({}, {}, []),
    ({"prompt_cache_hit_tokens": 0}, {"prompt_cache_hit_tokens": 0}, []),
    ({"prompt_cache_hit_tokens": True, "prompt_cache_miss_tokens": -1,
      "prompt_tokens_details": "private-invalid"}, {}, ["invalid_cache_usage"]),
    ({"prompt_cache_hit_tokens": 64, "prompt_tokens_details": {"cached_tokens": 32}},
     {"prompt_cache_hit_tokens": 64, "cached_tokens": 32}, ["conflicting_cache_usage"]),
])
def test_optional_cache_usage_is_safe_read_only_and_keeps_terminal_contract(
    usage, expected, warnings
):
    async def run():
        body = sse(chunk("正文"), chunk(finish="stop"), {
            "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, **usage},
        })
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

        async with ModelService(transport=httpx.MockTransport(respond)) as service:
            handle, actual = await invoke(service)
            plain, normal = await invoke(service, diagnostics=False)
        assert actual == normal
        assert actual[-1]["usage"] == {"prompt_tokens": 100, "completion_tokens": 10}
        assert actual[-1]["reason"] == "normal" and plain.diagnostics() == {}
        info = handle.diagnostics()
        assert info.get("cache_usage", {}) == expected
        assert info.get("diagnostic_warnings", []) == warnings
        assert info["transport_stats"]["trace_events"] == 0  # MockTransport不能证明复用。
        assert "trace" in requests[0].extensions and "trace" not in requests[1].extensions
        info.setdefault("cache_usage", {}).clear()
        info["transport_stats"]["tcp_connects"] = 999
        assert handle.diagnostics().get("cache_usage", {}) == expected
        assert handle.diagnostics()["transport_stats"]["tcp_connects"] == 0
        assert "private-invalid" not in repr(handle.diagnostics())
        assert "private-field" not in repr(handle.diagnostics())

    asyncio.run(run())


def test_trace_only_whitelists_names_and_never_copies_info():
    async def run():
        async def respond(request):
            trace = request.extensions["trace"]
            for event in ("connection.connect_tcp.started", "connection.connect_tcp.complete",
                          "connection.start_tls.started", "connection.start_tls.complete",
                          "http2.send_request_headers.started",
                          "http2.send_request_headers.complete",
                          "unknown.private-event"):
                await trace(event, {"host": "private-host", "headers": "private-key"})
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=sse(chunk("完成"), chunk(finish="stop")),
            )

        async with ModelService(transport=httpx.MockTransport(respond)) as service:
            handle, events = await invoke(service)
        info = handle.diagnostics()
        assert events[-1]["reason"] == "normal"
        assert info["transport_stats"] == {
            "trace_events": 6, "tcp_connects": 1, "tls_handshakes": 1, "requests_started": 1,
        }
        assert "private-" not in repr(info)
        assert "tls_complete" in [e["stage"] for e in info["timeline"]]
        assert info["started_monotonic_s"] > 0

    asyncio.run(run())


@pytest.mark.parametrize("expiry, expected_connections", [(0.02, 2), (300, 1)])
def test_real_tcp_trace_and_idle_expiry(expiry, expected_connections):
    async def run():
        accepted = []

        async def respond(reader, writer):
            accepted.append(1)
            try:
                while True:
                    await read_request(reader)
                    body = sse(chunk("相同响应"), chunk(finish="stop"))
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                                 + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                    await writer.drain()
            except asyncio.IncompleteReadError:
                pass

        async with local_server(respond) as address:
            settings = replace(profile(address), credential_ref=None)
            async with ModelService(keepalive_expiry_s=expiry) as service:
                first, _ = await invoke(service, settings=settings)
                await asyncio.sleep(0.05)
                second, _ = await invoke(service, settings=settings)
            assert len(accepted) == expected_connections
        assert first.diagnostics()["transport_stats"]["tcp_connects"] == 1
        assert second.diagnostics()["transport_stats"]["tcp_connects"] == expected_connections - 1
        assert second.diagnostics()["transport_stats"]["requests_started"] == 1
        assert second.diagnostics()["transport_stats"]["tls_handshakes"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), float("nan"), "300"])
def test_keepalive_expiry_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="空闲"):
        ModelService(keepalive_expiry_s=value)


def test_cancelled_call_does_not_invent_cache_usage():
    class Hanging(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chunk("首片"), done=False)
            await asyncio.Event().wait()

    async def run():
        async with ModelService(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Hanging()
        ))) as service:
            handle = service.start(
                [ChatMessage("user", "测试")], profile=profile(), diagnostics=True
            )
            events = handle.events()
            await anext(events)
            await handle.cancel()
            assert (await anext(events))["reason"] == "cancelled"
            assert "cache_usage" not in handle.diagnostics()

    asyncio.run(run())
