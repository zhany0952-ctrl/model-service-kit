# 其他Python项目：接入与旧调用入口改写

先按[安装手册](installation.md)把wheel装到目标项目的环境。只安装SDK不会自动替换你原来的LLM调用：第一次接入时，需要修改原调用入口，让它调用本包接口；业务上下文和业务输出结构不需要因此统一成NPC格式。

## 信息流与职责

```text
你的程序生成messages
  → 读取GUI已保存的配置快照（选哪个模型、地址、参数、凭据引用）
  → ModelService.start：内部取凭据、构造请求、直接访问模型服务
  → handle.events：正文增量／实际结束／故障
  → 你的程序解析业务结果、验证、保存或执行
```

配置GUI负责编辑和保存配置；ProfileStore负责读写配置元数据；ModelService负责通信，内部按引用取密钥。不需要先打开GUI，也没有额外本地HTTP中转。包不保存你的对话历史，不自动把上一轮回复加入下一轮；消息和上下文记忆由调用方控制，服务商的缓存／保留策略不由客户端决定。

## 1. 选择共享配置

```python
from pathlib import Path
from model_service import ProfileStore

store = ProfileStore()  # 同一系统用户的默认配置，和GUI默认一致。
# GUI使用自定义文件时，改为：
# store = ProfileStore(Path("my-profiles.json"))
profile = store.active_profile()  # 仅元数据，不读密钥，不调用模型。
# 或者：profile = store.get("my-saved-model")，不改变GUI活动选择。
```

相对路径按进程工作目录解析；实际项目建议把自定义配置路径解析为确定的绝对路径再传入。不要把API Key复制到源码、messages或日志。没有活动配置时明确失败，请先用GUI保存／选择，不要在程序里偷偷换模型。

## 2. 把原模型调用换成流式入口

原调用通常有“构造messages → 调用某SDK → 从返回对象取正文”三部分。保留第一部分的实际上下文，替换中间的调用和末尾的返回读取方式。例如：

```python
import asyncio
from model_service import ChatMessage, ModelService, ProfileStore


async def one_call(service, profile, messages):
    parts = []
    handle = service.start(messages, profile=profile)
    try:
        async with asyncio.timeout(60):  # 业务方自己的总等待保护，可按业务需要调整。
            async for event in handle.events():
                if event["type"] == "text_delta":
                    parts.append(event["text"])
                    # 业务方也可以在这里增量解析，而不是等全部收齐。
                elif event["type"] == "fault":
                    raise RuntimeError("调用故障：" + event["code"])
                elif event["type"] == "end" and event["reason"] != "normal":
                    raise RuntimeError("调用未正常完成：" + event["reason"])
        return "".join(parts)
    finally:
        await handle.cancel()  # 异常、超时或提前停止消费时，也释放本次响应。


async def run(inputs):
    async with ModelService(ProfileStore()) as service:
        for user_text in inputs:
            profile = service.active_profile()  # 新一轮读最新配置；本次调用固定快照。
            messages = [ChatMessage("user", user_text)]
            text = await one_call(service, profile, messages)
            # 在这里接回你原有的解析／验证／业务处理，不必使用NPC的区域契约。
            print(text)


# 只有业务方实际执行 asyncio.run(run([...])) 才调用模型，可能计费。
```

如果原项目使用`{"role": ..., "content": ...}`字典列表，可在入口转换为`[ChatMessage(m["role"], m["content"]) for m in original_messages]`；当前公共消息只支持system／user／assistant及纯文本content。多模态、工具消息、函数调用返回结构不是当前接口的通用能力，不能无转换地直接搬过来。

已有asyncio业务：直接await或消费异步生成器，不在运行中的事件循环内再调用`asyncio.run()`。同步应用若只偶尔调用，可在其顶层建立循环；多轮／高频调用应复用同一事件循环内的ModelService，退出时统一关闭。GUI或业务线程的调度仍由各自程序管理。

## 3. 必须改与不必改的部分

| 位置 | 接入要求 |
| --- | --- |
| 项目依赖 | 安装本地wheel；根据项目自己的依赖管理方式记录采用版本及wheel来源。不要把本包仅本地构建的名称直接当成已发布PyPI项目安装。 |
| 原模型调用入口 | 改为读取ProfileStore快照、start、消费events；原SDK返回对象不能直接当作本包事件读取。 |
| 字符串／JSON解析 | 可以保留原业务逻辑；若原先一次性解析，现在可先收齐正文再解析，或自行接入增量解析。 |
| 生成参数 | 优先在GUI保存；需要一次实验覆盖时，可用dataclasses.replace形成快照，不调用store.save就不会回写GUI。参数支持范围仍以所选服务为准。 |
| 认知引导、R区域、C7、业务schema | 本包不要求也不认识；NPC专用适配仍属于NPC项目，不带进通用wheel。 |
| 取消与异常 | 区分正常、截断、拒绝、取消和故障；不能把已有部分正文直接当完整成功；提前停止消费须cancel。 |
| 模型切换 | 下一次重新读取配置才取新快照；本次调用及其业务修正如须固定模型，由调用方保存同一快照。切换GUI不会取消已发起请求。 |

`start`的连接超时和读取空闲超时不是整次调用总截止；不断到达数据可持续保持读取。业务方需要总保护时如示例使用`asyncio.timeout`并在finally取消。不保证取消能终止服务端计算／计费。本包没有自动重试、故障自修复或自动模型切换。

## 4. 可运行示例与安全试验

[stream_from_config.py](../examples/stream_from_config.py)是完整命令行示例，默认预览，只有`--send`才发起一次请求。先在GUI保存配置并设为活动，或通过`--profile`选择指定配置；自定义文件必须让GUI和程序使用同一路径。

SDK的程序验证可以使用临时配置、回环受控服务或显式注入的httpx.MockTransport，不需要先连接付费模型。真实模型测试应由使用者明确确认目的地、发送内容和费用；安装包时不会自动做真实连接测试。

公共字段、诊断、可选内容预览、旧同步generate的兼容限制见[接口说明](interface.md)。新接入优先用本节的start／events；保留旧generate不代表将流式事件替换成旧同步返回对象，也不代表包会替应用管理上下文记忆。
