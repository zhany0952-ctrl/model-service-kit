"""客户端复用与请求隔离；本机TCP／受控传输不代表模型认知测试。"""

import asyncio
from dataclasses import replace

import httpx
import pytest
from test_streaming import chunk, local_server, profile, read_request, sse

from model_service import ChatMessage, ModelService


async def consume(service, settings):
    handle = service.start([ChatMessage("user", "观察")], profile=settings)
    return [event async for event in handle.events()]


def test_reuses_real_tcp_and_closes_pool_explicitly():
    async def run():
        accepted, requests = [], []
        disconnected = asyncio.Event()

        async def respond(reader, writer):
            accepted.append(1)
            try:
                while True:
                    requests.append(await read_request(reader))
                    body = sse(chunk("你好"), chunk(finish="stop"))
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                    await writer.drain()
            except asyncio.IncompleteReadError:
                disconnected.set()

        async with local_server(respond) as address:
            async with ModelService() as service:
                for _ in range(3):
                    assert (await consume(service, profile(address)))[-1]["reason"] == "normal"
                assert len(accepted) == 1 and len(requests) == 3
                assert not disconnected.is_set()
            await asyncio.wait_for(disconnected.wait(), 1)
            await service.aclose()  # 重复关闭安全。
            with pytest.raises(RuntimeError, match="关闭"):
                service.start([ChatMessage("user", "观察")], profile=profile(address))

    asyncio.run(run())


def test_request_headers_cookies_and_timeouts_are_not_shared():
    async def run():
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "set-cookie": "session_id=should-not-persist; Path=/",
                },
                content=sse(chunk("你好"), chunk(finish="stop")),
            )

        async with ModelService(transport=httpx.MockTransport(respond)) as service:
            await consume(
                service, replace(profile(), connect_timeout_seconds=1, read_timeout_seconds=2)
            )
            await consume(
                service,
                replace(
                    profile(),
                    credential_ref=None,
                    connect_timeout_seconds=3,
                    read_timeout_seconds=4,
                ),
            )
            assert requests[0].headers["authorization"] == "Bearer test-secret"
            assert "authorization" not in requests[1].headers
            assert "cookie" not in requests[1].headers
            assert requests[0].extensions["timeout"]["read"] == 2
            assert requests[1].extensions["timeout"]["read"] == 4
            assert not service._client.cookies

    asyncio.run(run())


def test_cancelling_one_call_keeps_other_calls_and_pool_available():
    class Hanging(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield sse(chunk("第一片"), done=False)
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    async def run():
        hanging, counter = Hanging(), 0

        def respond(request):
            nonlocal counter
            counter += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=hanging if counter == 1 else None,
                content=None if counter == 1 else sse(chunk("正常"), chunk(finish="stop")),
            )

        async with ModelService(transport=httpx.MockTransport(respond)) as service:
            handle = service.start([ChatMessage("user", "观察")], profile=profile())
            events = handle.events()
            await anext(events)
            other = asyncio.create_task(consume(service, profile()))
            await handle.cancel()
            assert hanging.closed
            assert (await anext(events))["reason"] == "cancelled"
            assert (await other)[-1]["reason"] == "normal"
            assert (await consume(service, profile()))[-1]["reason"] == "normal"
            assert not service._client.is_closed

    asyncio.run(run())


def test_service_close_cancels_outstanding_handles():
    async def run():
        service = ModelService()
        handle = service.start([ChatMessage("user", "观察")], profile=profile())
        await asyncio.gather(service.aclose(), service.aclose())
        events = [event async for event in handle.events()]
        assert events[-1] == {"type": "end", "reason": "cancelled"}
        assert not service._handles

    asyncio.run(run())


def test_service_does_not_share_client_between_event_loops():
    async def run():
        async with ModelService(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=sse(chunk("正常"), chunk(finish="stop")),
                )
            )
        ) as service:
            await consume(service, profile())

            async def another_loop():
                with pytest.raises(RuntimeError, match="事件循环"):
                    service.start([ChatMessage("user", "观察")], profile=profile())

            await asyncio.to_thread(asyncio.run, another_loop())

    asyncio.run(run())
