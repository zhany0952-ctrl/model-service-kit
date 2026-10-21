"""其他Python项目的调用示例：默认只预览，--send才发送一次请求。"""

from __future__ import annotations

import argparse
import asyncio
import math
from pathlib import Path

from model_service import ChatMessage, ModelService, ProfileStore


async def stream_once(service, profile, messages, *, total_timeout_s=60):
    """业务方提供消息；本例只演示正文及实际终态，不解析业务JSON。"""
    if not math.isfinite(total_timeout_s) or total_timeout_s <= 0:
        raise ValueError("总等待时间必须为正的有限秒数。")
    handle = service.start(messages, profile=profile)
    try:
        async with asyncio.timeout(total_timeout_s):
            async for event in handle.events():
                if event["type"] == "text_delta":
                    print(event["text"], end="", flush=True)
                elif event["type"] == "fault":
                    raise RuntimeError("模型调用故障：" + event["code"])
                elif event["type"] == "end" and event["reason"] != "normal":
                    raise RuntimeError("模型调用未正常完成：" + event["reason"])
    finally:
        await handle.cancel()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config", type=Path, help="与GUI选择的配置文件一致；省略则用当前用户配置。"
    )
    result.add_argument("--profile", help="指定已保存配置标识；省略则读取活动配置。")
    result.add_argument("--message", default="请简短回复：连接正常。")
    result.add_argument("--total-timeout", type=float, default=60)
    result.add_argument("--send", action="store_true", help="明确发送一次真实请求，可能计费。")
    return result


async def run(args):
    store = ProfileStore(args.config)
    profile = store.get(args.profile) if args.profile else store.active_profile()
    print(f"配置：{profile.profile_id}；模型：{profile.model}")
    if not args.send:
        print("仅预览，未读取密钥或调用模型。确认目的地、内容与费用后添加--send。")
        return
    async with ModelService(store) as service:
        await stream_once(
            service, profile, [ChatMessage("user", args.message)],
            total_timeout_s=args.total_timeout,
        )
    print()


if __name__ == "__main__":
    try:
        asyncio.run(run(parser().parse_args()))
    except (ValueError, KeyError, OSError, RuntimeError, TimeoutError) as exc:
        # 不复制配置内容、网络错误原文或密钥；详细业务日志由接入方设计。
        print("\n调用未完成：" + type(exc).__name__)
        raise SystemExit(1) from None
