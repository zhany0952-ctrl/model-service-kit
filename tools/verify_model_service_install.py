"""在干净venv中执行：只用临时配置和回环受控HTTP，不读取真实配置／密钥。"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


def verify(example):
    import httpx
    import keyring

    import model_service
    from model_service import ChatMessage, CredentialStore, ModelProfile, ModelService, ProfileStore

    prefix = Path(sys.prefix).resolve()
    assert sys.flags.isolated and sys.prefix != sys.base_prefix, "须以干净venv的python -I执行。"
    for module in (model_service, httpx, keyring):
        assert Path(module.__file__).resolve().is_relative_to(prefix)
    assert importlib.util.find_spec("npc_agent") is None
    assert importlib.util.find_spec("pydantic") is None
    assert importlib.metadata.version("model-service-kit") == "0.1.0"
    direct = json.loads(importlib.metadata.distribution("model-service-kit").read_text(
        "direct_url.json"
    ))
    assert "archive_info" in direct and not direct.get("dir_info", {}).get("editable")
    assert "include-system-site-packages = false" in (prefix / "pyvenv.cfg").read_text().lower()
    requests = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, ConnectionAbortedError):
                pass  # 本机取消验证可在下一次读取请求行时断开，不隐藏其他服务异常。

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            if payload["model"] == "http-error":
                self.send_response(401)
                self.send_header("Content-Length", "16")
                self.end_headers()
                self.wfile.write(b"private-response")
                return
            finish = "length" if payload["model"] == "truncated" else "stop"
            first = 'data: ' + json.dumps({
                "choices": [{"index": 0, "delta": {"content": "安装"}}]
            }) + '\n\n'
            rest = "data: " + json.dumps({"choices": [{
                "index": 0, "delta": {"content": "验证通过"}, "finish_reason": finish
            }], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})
            rest += "\n\ndata: [DONE]\n\n"
            a, b = first.encode(), rest.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(a) + len(b)))
            self.end_headers()
            try:
                self.wfile.write(a)
                self.wfile.flush()
                if payload["model"] == "slow":
                    release.wait(5)
                self.wfile.write(b)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    class NoSystemCredentials:
        def get_password(self, *_):
            raise AssertionError("不能读取系统凭据。")

        def set_password(self, *_):
            raise AssertionError("不能写系统凭据。")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    checks = ["isolated_non_editable_wheel", "dependencies_inside_venv", "no_npc_dependency"]
    print("PASS: installed imports and metadata", flush=True)
    try:
        with TemporaryDirectory(prefix="model-service-install-") as temp:
            work = Path(temp)
            store = ProfileStore(work / "profiles.json", credentials=CredentialStore(
                backend=NoSystemCredentials()
            ))
            profile = ModelProfile(
                profile_id="controlled", name="安装验证临时配置", model="normal",
                base_url=f"http://127.0.0.1:{server.server_port}/v1", json_mode=False,
            )
            store.save(profile)
            store.activate(profile.profile_id)
            assert store.active_profile() == profile
            checks.append("temporary_configuration_roundtrip")
            print("PASS: temporary configuration", flush=True)

            # 真正创建Tk控件，窗口隐藏；不点击连接测试，也不接触用户配置。
            import tkinter as tk

            from model_service.gui import ModelConfigApp

            window = tk.Tk()
            window.withdraw()
            try:
                view = ModelConfigApp(window, store)
                view.choice.set(profile.profile_id)
                view.load(_confirm=False)  # 测试明确批准丢弃临时草稿，避免隐藏确认框等待。
                assert view.values["model"].get() == "normal"
                window.update_idletasks()
            finally:
                window.destroy()
            checks.append("installed_gui_hidden_tk_and_profile_load")
            print("PASS: hidden installed GUI", flush=True)
            command = prefix / ("Scripts/model-service-config.exe" if os.name == "nt"
                                else "bin/model-service-config")
            for cmd in ([str(command), "--help"],
                        [sys.executable, "-I", "-m", "model_service.gui", "--help"]):
                subprocess.run(cmd, cwd=work, capture_output=True, check=True, timeout=10)
            checks.append("installed_console_and_module_entrypoints")
            print("PASS: installed entrypoints", flush=True)

            async def calls():
                async with ModelService(store) as service:
                    messages = [ChatMessage("user", "受控安装验证，不是真实模型请求。")]
                    for name in ("normal", "normal", "truncated", "http-error", "slow"):
                        handle = service.start(messages, profile=replace(profile, model=name))
                        events = []
                        try:
                            async with asyncio.timeout(5):
                                async for event in handle.events():
                                    events.append(event)
                                    if name == "slow" and event["type"] == "text_delta":
                                        await handle.cancel()
                        finally:
                            await handle.cancel()
                        terminal = events[-1]
                        if name == "normal":
                            assert (terminal["type"] == "end"
                                    and terminal["reason"] == "normal"), terminal
                            assert "".join(e["text"] for e in events
                                           if e["type"] == "text_delta") == "安装验证通过"
                            assert terminal["usage"]["total_tokens"] == 6
                        elif name == "http-error":
                            assert terminal["type"] == "fault" and terminal["http_status"] == 401
                            assert "private-response" not in json.dumps(events)
                        else:
                            reason = "cancelled" if name == "slow" else "truncated"
                            assert terminal["type"] == "end" and terminal["reason"] == reason
                        checks.append("installed_stream_" + name)
                        print("PASS: controlled stream " + name, flush=True)

            asyncio.run(calls())
            args = [sys.executable, "-I", str(example), "--config", str(store.path)]
            subprocess.run(args, cwd=work, capture_output=True, check=True, timeout=10)
            assert len(requests) == 5  # 示例默认预览不触发模型请求。
            subprocess.run(args + ["--send"], cwd=work, capture_output=True,
                           check=True, timeout=10)
            assert len(requests) == 6
            assert requests[0]["messages"] == requests[1]["messages"]
            assert all(r["stream"] is True for r in requests)
            checks.append("other_project_example_preview_and_controlled_call")
            print("PASS: other-project example", flush=True)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {"status": "passed", "python": sys.version, "prefix": str(prefix),
            "package_path": str(Path(model_service.__file__).resolve()),
            "version": "0.1.0", "checks": checks, "controlled_http_requests": len(requests),
            "dependencies": {d.metadata["Name"]: d.version
                             for d in importlib.metadata.distributions()},
            "real_model_requests": 0, "real_user_credentials_accessed": False}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    faulthandler.dump_traceback_later(45, repeat=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit("报告路径已存在，请使用新路径。")
    report = verify(args.example.resolve())
    faulthandler.cancel_dump_traceback_later()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
