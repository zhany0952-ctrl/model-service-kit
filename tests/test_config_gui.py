"""真实Tk控件操作测试：窗口隐藏，配置及凭据均使用隔离替身。"""

from __future__ import annotations

import json
import time
import tkinter as tk

import httpx
import pytest

from model_service import CredentialStore, ModelService, ProfileStore
from model_service.gui import ModelConfigApp


@pytest.fixture
def app(tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr("model_service.gui.messagebox.showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: True)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("当前环境没有Tk显示服务，GUI功能未在此环境验证。")
    root.withdraw()

    # 后端不可用，正常测试显式选仅进程；不调用真实系统凭据库。
    class UnavailableBackend:
        def set_password(self, *_):
            raise RuntimeError("private-backend-error")

    store = ProfileStore(
        tmp_path / "profiles.json", credentials=CredentialStore(backend=UnavailableBackend())
    )
    view = ModelConfigApp(root, store)
    try:
        yield view, store, errors
    finally:
        root.destroy()


def fill(view, *, profile_id="local", key=""):
    for name, value in {
        "profile_id": profile_id,
        "name": "本地配置",
        "model": "controlled-model",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": key,
    }.items():
        view.values[name].set(value)


def test_gui_local_save_activate_edit_and_delete(app):
    view, store, errors = app
    fill(view)
    view.buttons["save"].invoke()
    view.buttons["activate"].invoke()
    assert not errors and store.active_profile().model == "controlled-model"
    view.values["name"].set("改名")
    view.buttons["save"].invoke()
    assert store.get("local").name == "改名"
    view.buttons["delete"].invoke()
    assert store.list_profiles() == [] and not errors


def test_gui_network_key_session_and_load_never_displays_key(app):
    view, store, errors = app
    fill(view, profile_id="network", key="entered-secret")
    view.values["base_url"].set("https://example.test/v1")
    view.mode.set("仅本次进程")
    view.buttons["save"].invoke()
    assert not errors and view.values["api_key"].get() == ""
    saved = store.get("network")
    assert store.credentials.get(saved.credential_ref) == "entered-secret"
    assert "entered-secret" not in store.path.read_text(encoding="utf-8")
    view.load()
    assert view.values["api_key"].get() == "" and view.entries["api_key"].cget("show")
    view.values["name"].set("换名字，不替换密钥")
    view.save()
    assert store.get("network").credential_ref == saved.credential_ref


def test_gui_system_failure_visible_and_no_file_created(app):
    view, store, errors = app
    fill(view, key="entered-secret")
    view.save()
    assert errors and not store.path.exists()
    assert "entered-secret" not in repr(errors) and "private-backend-error" not in repr(errors)
    assert view.values["api_key"].get() == "entered-secret"  # 允许用户显式改选保存方式。
    view.mode.set("仅本次进程")
    view.save()
    assert store.get("local").credential_ref.startswith("session:")


def test_gui_optional_settings_and_advanced_layout(app):
    view, store, errors = app
    fill(view)
    view.show_advanced.set(True)
    view.toggle_advanced()
    view.values["temperature"].set("0.3")
    view.values["max_output_tokens"].set("600")
    view.values["thinking_json"].set('{"reasoning_effort":"low"}')
    view.save()
    assert not errors
    assert store.get("local").generation_parameters() == {
        "temperature": 0.3,
        "max_completion_tokens": 600,
        "reasoning_effort": "low",
    }
    view.update_idletasks()
    assert view.winfo_reqwidth() <= 1350 and view.winfo_reqheight() <= 950


def test_gui_unsaved_changes_not_silently_activated(app):
    view, store, errors = app
    fill(view)
    view.save()
    view.values["model"].set("not-saved")
    view.activate()
    assert errors and "先保存" in errors[-1][1]
    with pytest.raises(ValueError, match="活动"):
        store.active_profile()


def test_gui_clear_key_and_new_resets_optional_values(app):
    view, store, errors = app
    fill(view, key="entered-secret")
    view.mode.set("仅本次进程")
    view.save()
    view.clear_key.set(True)
    view.save()
    assert store.get("local").credential_ref is None and not errors
    view.new()
    assert view.values["profile_id"].get() == "" and view.values["temperature"].get() == ""
    assert view.values["connect_timeout_seconds"].get() == "10"


def test_gui_legacy_is_blocked_until_confirmed_migration(app, monkeypatch):
    view, store, errors = app
    store.path.write_text(
        json.dumps(
            {
                "active_profile_id": "old",
                "profiles": [
                    {
                        "profile_id": "old",
                        "name": "旧配置",
                        "base_url": "https://example.test/v1",
                        "model": "old-model",
                        "api_key": "legacy-secret",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = store.path.read_bytes()
    view.refresh()
    assert str(view.buttons["save"]["state"]) == "disabled"
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: False)
    view.migrate()
    assert store.path.read_bytes() == before
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: True)
    view.mode.set("仅本次进程")
    view.buttons["migrate"].invoke()
    assert not errors and not store.is_legacy()
    assert "legacy-secret" not in store.path.read_text(encoding="utf-8")
    assert str(view.buttons["save"]["state"]) == "normal"


def test_gui_delete_cancel_leaves_configuration(app, monkeypatch):
    view, store, _ = app
    fill(view)
    view.save()
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: False)
    view.delete()
    assert store.get("local").model == "controlled-model"


def test_gui_zero_timeout_rejected_not_replaced_by_default(app):
    view, store, errors = app
    fill(view)
    view.values["connect_timeout_seconds"].set("0")
    view.save()
    assert errors and not store.path.exists()


def test_gui_loading_reference_does_not_read_vault(app, monkeypatch):
    view, store, errors = app
    fill(view, key="entered-secret")
    view.mode.set("仅本次进程")
    view.save()

    def forbid(*_):
        pytest.fail("GUI加载配置不得读取密钥")

    monkeypatch.setattr(store.credentials, "get", forbid)
    view.load()
    assert not errors and view.values["api_key"].get() == ""


def test_gui_path_help_and_field_tooltips(app):
    view, store, _ = app
    assert str(store.path.resolve()) in view.config_path.get()
    assert all(name in view.tooltips for name in view.values)
    assert "11434" in view.tooltips["base_url"].text
    tip = view.tooltips["base_url"]
    tip.show()
    assert tip.window is not None
    tip.hide()
    view.buttons["help"].invoke()
    assert view.help_window.winfo_exists()


def test_gui_key_actions_are_mutually_exclusive(app):
    view, _, _ = app
    fill(view, key="new-secret")
    view.clear_key.set(True)
    assert view.values["api_key"].get() == ""
    view.values["api_key"].set("replacement-secret")
    assert not view.clear_key.get()


def test_gui_probe_requires_saved_unchanged_configuration(app, monkeypatch):
    view, store, errors = app
    fill(view)
    view.test_connection()
    assert errors and "保存" in errors[-1][1]
    view.save()
    view.values["model"].set("unsaved")
    view.test_connection()
    assert "保存" in errors[-1][1]
    assert store.get("local").model == "controlled-model"


def test_gui_probe_confirmation_cancel_does_not_start(app, monkeypatch):
    view, store, errors = app
    fill(view)
    view.save()
    before = store.path.read_bytes()
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: False)
    view.buttons["test"].invoke()
    assert view.probe_job is None and not errors
    assert before == store.path.read_bytes()


def test_gui_switch_file_preserves_session_store_and_cancelled_draft(app, monkeypatch, tmp_path):
    view, store, _ = app
    fill(view, key="draft-secret")
    target = tmp_path / "other.json"
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: False)
    view.use_config(target)
    assert view.store is store and view.values["api_key"].get() == "draft-secret"
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: True)
    view.use_config(target)
    assert view.store.path == target and view.store.credentials is store.credentials
    assert view.values["api_key"].get() == "" and not target.exists()


def test_gui_no_discard_unsaved_on_new_load_or_close(app, monkeypatch):
    view, _, _ = app
    fill(view)
    view.save()
    view.values["name"].set("未保存的名字")
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: False)
    view.new()
    view.load()
    view.close()
    assert view.values["name"].get() == "未保存的名字" and view.winfo_exists()


def finish_probe(view):
    deadline = time.monotonic() + 3
    while view.probe_job is not None and time.monotonic() < deadline:
        view.update()
        time.sleep(0.005)
    assert view.probe_job is None


def test_gui_initialization_never_asks_discard(app, monkeypatch):
    view, store, _ = app
    asked = []
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: asked.append(a))
    other = ModelConfigApp(view.winfo_toplevel(), store)
    assert not asked
    other.destroy()


def test_gui_probe_worker_uses_saved_config_and_does_not_write(app, monkeypatch):
    view, store, errors = app
    fill(view)
    view.save()
    before = store.path.read_bytes()
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        chunk = {"choices": [{"index": 0, "delta": {"content": "已连接"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    monkeypatch.setattr(
        "model_service.gui.ModelService",
        lambda store: ModelService(store, transport=httpx.MockTransport(handler)),
    )
    view.buttons["test"].invoke()
    assert str(view.buttons["test"]["state"]) == "disabled"
    assert str(view.picker["state"]) == "disabled"
    finish_probe(view)
    assert "通过" in view.test_status.get() and "首片" in view.test_status.get()
    assert "HTTP状态：200" in view.probe_metrics.get()
    assert "响应头" in view.probe_log.get("1.0", "end")
    assert not errors and len(calls) == 1 and before == store.path.read_bytes()
    assert calls[0]["model"] == "controlled-model"
    with pytest.raises(ValueError, match="活动"):
        store.active_profile()
    view.values["model"].set("changed")
    assert "未验证" in view.test_status.get()


def test_gui_probe_cancel_and_close_release_worker(app, monkeypatch):
    view, store, _ = app
    fill(view)
    view.save()

    async def handler(_):
        import asyncio

        await asyncio.Event().wait()

    monkeypatch.setattr(
        "model_service.gui.ModelService",
        lambda store: ModelService(store, transport=httpx.MockTransport(handler)),
    )
    view.test_connection()
    view.buttons["cancel"].invoke()
    finish_probe(view)
    assert "已取消" in view.test_status.get()
    destroyed = []
    monkeypatch.setattr(view.winfo_toplevel(), "destroy", lambda: destroyed.append(True))
    view.test_connection()
    job = view.probe_job
    view.close()
    finish_probe(view)
    job.thread.join(timeout=1)
    assert destroyed and not job.thread.is_alive()


def test_gui_invalid_config_switch_leaves_original(app, tmp_path):
    view, store, errors = app
    target = tmp_path / "bad.json"
    target.write_text("not-json", encoding="utf-8")
    view.use_config(target)
    assert view.store is store and errors


def test_gui_probe_panel_is_on_right_with_scrollable_timeline(app):
    view, _, _ = app
    view.update_idletasks()
    assert int(view.probe_panel.grid_info()["column"]) == 2
    assert view.buttons["test"].master is view.probe_controls
    assert view.buttons["cancel"].master is view.probe_controls
    assert str(view.probe_log.cget("state")) == "disabled"


def test_gui_progress_panel_keeps_evidence_and_invalidates_old_result(app):
    view, _, _ = app
    view._render_progress(
        {
            "stage": "receiving_reasoning",
            "elapsed_s": 2.1,
            "http_status": 200,
            "content_type": "text/event-stream",
            "received_bytes": 160,
            "event_count": 3,
            "reasoning_chars": 12,
            "reasoning_chunks": 2,
            "text_chars": 0,
            "text_chunks": 0,
            "timeline": [{"stage": "response_headers", "elapsed_s": 0.2}],
        }
    )
    assert "思考" in view.probe_stage.get() and "200" in view.probe_metrics.get()
    assert "12" in view.probe_metrics.get()
    assert "响应头" in view.probe_log.get("1.0", "end")
    view.values["model"].set("changed")
    assert "未验证" in view.test_status.get()


def test_gui_thinking_presets_roundtrip_and_preserve_custom_values(app):
    view, store, errors = app
    fill(view)
    view.thinking_mode.set("请求关闭（none）")
    view.apply_thinking_mode()
    view.save()
    assert store.get("local").generation_parameters()["reasoning_effort"] == "none"
    view.new()
    view.choice.set("local")
    view.load()
    assert view.thinking_mode.get() == "请求关闭（none）"
    view.values["thinking_json"].set('{"reasoning_effort":"low","custom_flag":true}')
    assert view.thinking_mode.get() == "自定义JSON"
    view.thinking_mode.set("请求开启（medium）")
    view.apply_thinking_mode()
    view.save()
    assert store.get("local").generation_parameters() == {
        "reasoning_effort": "medium",
        "custom_flag": True,
    }
    view.thinking_mode.set("模型默认")
    view.apply_thinking_mode()
    view.save()
    assert store.get("local").generation_parameters() == {} and not errors


def test_gui_invalid_thinking_json_is_not_silently_replaced(app):
    view, _, errors = app
    view.values["thinking_json"].set("{not-json")
    view.thinking_mode.set("请求开启（medium）")
    view.apply_thinking_mode()
    assert errors and view.values["thinking_json"].get() == "{not-json"


def test_gui_content_preview_is_opt_in_and_cleared_when_hidden(app):
    view, _, _ = app
    assert not view.show_content.get()
    view._render_content({"text": "不得显示", "reasoning": "不得显示", "truncated": False})
    assert "不得显示" not in view.content_texts["text"].get("1.0", "end")
    view.show_content.set(True)
    view.toggle_content()
    view._render_content({"text": "受控回答", "reasoning": "受控思考", "truncated": False})
    assert "受控回答" in view.content_texts["text"].get("1.0", "end")
    assert "受控思考" in view.content_texts["reasoning"].get("1.0", "end")
    assert "受控思考" not in view.probe_log.get("1.0", "end")
    view.show_content.set(False)
    view.toggle_content()
    assert "受控回答" not in view.content_texts["text"].get("1.0", "end")


def test_gui_worker_preview_is_separate_from_logs_and_config(app, monkeypatch):
    view, store, errors = app
    fill(view, key="entered-secret")
    view.mode.set("仅本次进程")
    view.thinking_mode.set("请求关闭（none）")
    view.apply_thinking_mode()
    view.save()
    before = store.path.read_bytes()
    records = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "受控思考 entered-secret"},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "受控正文 entered-secret"},
                    "finish_reason": "stop",
                }
            ]
        },
    ]
    body = "".join("data: " + json.dumps(r) + "\n\n" for r in records) + "data: [DONE]\n\n"
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    monkeypatch.setattr(
        "model_service.gui.ModelService",
        lambda store: ModelService(store, transport=httpx.MockTransport(handler)),
    )
    view.show_content.set(True)
    view.toggle_content()
    view.test_connection()
    assert str(view.content_toggle["state"]) == "disabled"
    finish_probe(view)
    assert calls[0]["reasoning_effort"] == "none"
    assert "受控正文" in view.content_texts["text"].get("1.0", "end")
    assert "受控思考" in view.content_texts["reasoning"].get("1.0", "end")
    for text in (*view.content_texts.values(), view.probe_log):
        assert "entered-secret" not in text.get("1.0", "end")
    assert "受控思考" not in view.probe_log.get("1.0", "end")
    assert "仍收到思考" in view.probe_hint.get()
    assert store.path.read_bytes() == before and not errors


def test_gui_custom_json_and_default_cancel_preserve_input(app, monkeypatch):
    view, _, errors = app
    view.values["thinking_json"].set('{"reasoning_effort":[]}')
    assert view.thinking_mode.get() == "自定义JSON"
    monkeypatch.setattr("model_service.gui.messagebox.askyesno", lambda *a, **k: False)
    view.thinking_mode.set("模型默认")
    view.apply_thinking_mode()
    assert view.values["thinking_json"].get() == '{"reasoning_effort":[]}' and not errors


def test_gui_expanded_preview_and_advanced_fit_layout(app):
    view, _, _ = app
    view.show_content.set(True)
    view.toggle_content()
    view.show_advanced.set(True)
    view.toggle_advanced()
    view.update_idletasks()
    assert view.winfo_reqwidth() <= 1350 and view.winfo_reqheight() <= 1000
