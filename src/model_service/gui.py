"""中文模型配置界面；仅显式连接测试会调用模型，不自动迁移凭据。"""

from __future__ import annotations

import argparse
import json
import tkinter as tk
from pathlib import Path
from queue import Empty
from time import perf_counter
from tkinter import filedialog, messagebox, ttk

from .client import ModelService
from .config import ModelProfile, ProfileStore, _json
from .credentials import CredentialError
from .probe import PROBE_MAX_TOKENS, PROBE_TIMEOUT_SECONDS, ProbeJob, diagnostic_hint

MODES = {"系统凭据库": "system", "仅本次进程": "session"}
LIMIT_FIELDS = {
    "标准字段（max_completion_tokens）": "max_completion_tokens",
    "兼容字段（max_tokens）": "max_tokens",
}

THINKING_MODES = {
    "模型默认": None,
    "请求开启（medium）": "medium",
    "请求关闭（none）": "none",
    "自定义JSON": "custom",
}

STAGE_NAMES = {
    "prepared": "已准备独立测试请求",
    "credential_lookup": "正在读取凭据（不显示密钥）",
    "waiting_headers": "正在发起请求／等待HTTP响应头",
    "tcp_started": "开始建立TCP连接（含本机域名解析等待）",
    "tcp_complete": "TCP连接建立完成",
    "tcp_failed": "TCP连接建立失败",
    "tls_started": "开始TLS握手",
    "tls_complete": "TLS握手完成",
    "tls_failed": "TLS握手失败",
    "request_headers_started": "开始发送HTTP请求头",
    "request_headers_sent": "HTTP请求头已发送",
    "request_body_sent": "HTTP请求体已发送",
    "response_headers_wait": "等待服务响应头（不等于纯模型计算）",
    "response_headers_received": "底层已接收响应头",
    "response_headers": "已收到HTTP响应头",
    "first_data": "已收到第一块响应体数据",
    "first_event": "已解析首个有效流式事件",
    "first_reasoning": "已收到首个独立思考片段（仅统计）",
    "receiving_reasoning": "正在接收思考片段，等待正文",
    "first_text": "已收到首片正文",
    "receiving_text": "正在接收正文",
    "finish_reason": "已收到服务结束原因，等待流关闭",
    "closed": "本次响应已释放／调用已终止",
}

ERROR_NAMES = {
    "ConnectTimeout": "ConnectTimeout（连接超时）",
    "ReadTimeout": "ReadTimeout（读取空闲超时）",
    "WriteTimeout": "WriteTimeout（发送超时）",
    "PoolTimeout": "PoolTimeout（连接池等待超时）",
    "ConnectError": "ConnectError（连接失败）",
    "ReadError": "ReadError（读取失败）",
    "WriteError": "WriteError（发送失败）",
    "RemoteProtocolError": "RemoteProtocolError（远端传输协议异常）",
    "LocalProtocolError": "LocalProtocolError（本地传输协议异常）",
    "CredentialError": "CredentialError（凭据不可用）",
    "ProtocolFault": "ProtocolFault（流式协议不兼容）",
    "InvalidEvent": "InvalidEvent（事件格式无效）",
}

FIELD_HELP = {
    "profile_id": "配置的唯一标识，例如 local-qwen。保存后不可改；需新标识请点新增。不是模型名称。",
    "name": "供人辨认的名称，例如 本地Qwen 或 网络模型；不发送给模型服务。",
    "base_url": (
        "填写兼容服务根地址，不附加 /chat/completions。\n"
        "本地Ollama示例：http://127.0.0.1:11434/v1\n"
        "网络示例：https://你的服务域名/v1（以服务文档为准）。本地须先启动模型服务。"
    ),
    "model": (
        "填写服务实际提供的模型名称。本地以 ollama list 为准，"
        "例如 qwen3.5:9b（仅示例，不表示已安装）；网络填写供应商提供的模型标识。"
    ),
    "api_key": (
        "新建时本地无认证可留空；编辑时留空保留原密钥引用。"
        "输入新密钥会替换引用；勾选清除会清空本输入。不会回显旧密钥。"
    ),
    "temperature": "可选，0～2，例如0.2；留空不发送，采用服务默认。不同模型可能不支持该参数。",
    "max_output_tokens": (
        "可选正整数，例如800；留空不发送，采用服务默认，不代表无限输出。"
        "思考模型可能将推理token计入上限，按服务文档选择字段。"
    ),
    "connect_timeout_seconds": (
        "建立连接的等待秒数，默认10。不是NPC整轮性能目标，也不表示模型必须10秒完成。"
    ),
    "read_timeout_seconds": (
        "连续没有网络数据时的等待秒数，默认60。不是整次调用总时限；连接测试另有30秒总时限。"
    ),
    "thinking_json": (
        "可选标准JSON对象，例如 {}；留空不发送。仅填写所选服务明确支持的思考参数，"
        "不填写密钥、消息或流式协议字段。下拉预设写入兼容参数，不保证所有服务支持。"
    ),
}

GUI_HELP = """模型配置与独立连接测试

一、这是什么
本窗口负责保存配置和密钥。不会启动Ollama、下载模型或自动改写其他项目。
保存、设为活动都不请求模型；只有确认“连接测试”才会真实调用，网络模型可能计费。

二、本地模型
先启动Ollama，并确认 ollama list 中已有模型。
服务根地址示例：http://127.0.0.1:11434/v1
模型名称填写列表中的完整名称。本地无认证时密钥留空。

三、网络模型
服务根地址、模型名称以提供方文档为准。远程带密钥请求要求HTTPS。
例如地址可为 https://你的服务域名/v1，不要再附加 /chat/completions。
这里只支持当前兼容的纯文本流式协议，不承诺所有服务都可用。

四、操作顺序
新增／选择配置 → 填写 → 保存 → 按需连接测试 → 按需设为活动模型。
保存不等于连接成功；活动配置也不等于已验证。测试不改变活动选择。
鼠标移到字段名称、输入框或按钮，或用键盘聚焦输入框，可查看详细说明。

五、密钥与文件
普通JSON明文保存地址、模型和参数，但密钥只存随机引用。
“系统凭据库”由操作系统保护，适合GUI关闭后由NPC等其他进程使用。
“仅本次进程”只在当前进程内存中有效，关闭后不会恢复，也不供独立NPC进程共享。
密钥留空保留；输入新值替换；勾选清除取消认证。删除配置不立即清理历史凭据。
系统凭据库不是对同用户恶意程序的绝对防护；运行时仍需在内存中使用密钥。
文件位置显示在窗口顶部。“选择文件”打开已有配置；“新建文件”选择新路径，保存时才创建。
切换文件不复制配置或凭据；不会静默丢弃未保存输入。

六、连接测试
只测试当前已保存且未修改的配置，单条独立短消息，不传NPC人设、记忆或历史。
本次输出上限取原值与64的较小值，保留其他生成设置；不会写回配置。
总等待上限30秒，连接／读取仍受配置超时约束；不重试。
收到非空正文且正常结束才通过；不要求逐字回答。截断、拒绝、断流均不算通过。
显示首片正文耗时、总耗时和字符数，默认不采集回复内容；生成原文不进入诊断日志。
右侧诊断面板实时显示HTTP状态、响应类型、响应体字节和事件数、思考／正文统计及里程碑时间线。
首块数据和思考片段不等于首片正文；没有正文不等于完全无响应。
响应体字节为HTTP客户端解压后的数据量，不是TCP包大小。未收到的阶段显示未知，不猜测模型在加载或排队。
网络／超时显示安全的异常类型和HTTP分类建议，不展示服务原始错误体或响应头。
思考预算不足造成截断不表示地址不通；这不是NPC质量验收或性能基准。
取消只保证本地停止接收，不能保证远端停止生成或计费。
测试不写NPC状态；服务商是否保留请求由服务商政策决定。

七、思考请求与可选内容
高级配置增加“思考请求”：默认、请求开启（medium）、请求关闭（none）或自定义JSON。
预设实际写入reasoning_effort，仍由服务和模型决定支持程度；不会自动查询能力或重试。
开启／关闭保留其他JSON参数并移除嵌套reasoning.effort避免冲突；恢复默认会清空思考JSON。
自定义参数可能与预设不兼容，请按服务文档配置。修改后先保存再测试。
请求关闭后仍收到独立思考字段会提示；没有思考字段不证明模型内部完全没有推理。
测试前勾选右侧“采集并查看返回内容”，可在两个页签分别查看正文和服务提供的思考字段。
不勾选则不采集；勾选后需要重新测试，不能恢复此前未采集的内容。运行中选项固定。
各最多4096字符，只在内存中预览；新测试清空旧内容，取消勾选或关闭窗口也清空。
已知API密钥原值及其尾部分片会遮蔽，但这不是完整敏感信息过滤，预览仍可能含敏感内容。
不自动保存、复制或写NPC记忆；独立思考字段不代表能访问模型全部内部计算。
"""


class Tooltip:
    """悬停或键盘聚焦显示说明；退出及销毁时清理定时器。"""

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.window = self.timer = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<FocusIn>", self.schedule, add="+")
        for event in ("<Leave>", "<FocusOut>", "<ButtonPress>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.timer = self.widget.after(450, self.show)

    def show(self):
        self.timer = None
        if self.window is not None or not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        ttk.Label(self.window, text=self.text, wraplength=440, padding=10, relief="solid").pack()
        self.window.update_idletasks()
        x = min(
            self.widget.winfo_rootx() + 12,
            self.widget.winfo_screenwidth() - self.window.winfo_reqwidth() - 8,
        )
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        if y + self.window.winfo_reqheight() > self.widget.winfo_screenheight():
            y = max(0, self.widget.winfo_rooty() - self.window.winfo_reqheight())
        self.window.geometry(f"+{max(0, x)}+{y}")

    def hide(self, _event=None):
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class ModelConfigApp(ttk.Frame):
    def __init__(self, root, store: ProfileStore | None = None, *, embedded=False):
        super().__init__(root, padding=16)
        self.store = store if store is not None else ProfileStore()
        self.loaded = None
        self.probe_job = None
        self.probe_profile_id = None
        self.probe_thinking_request = None
        self.probe_started = None
        self.probe_last_progress = {}
        self.probe_logged = 0
        self.probe_poll = None
        self.close_pending = False
        self.help_window = None
        self.tooltips = {}
        self.grid(sticky="nsew")
        if not embedded:
            root.title("模型服务配置：本地／网络")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        if not embedded:
            root.protocol("WM_DELETE_WINDOW", self.close)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=1)
        self.values = {
            name: tk.StringVar(self)
            for name in (
                "profile_id",
                "name",
                "base_url",
                "model",
                "api_key",
                "temperature",
                "max_output_tokens",
                "connect_timeout_seconds",
                "read_timeout_seconds",
                "thinking_json",
            )
        }
        self.entries = {}
        self.choice = tk.StringVar(self)
        self.mode = tk.StringVar(self, value="系统凭据库")
        self.limit_field = tk.StringVar(self, value=next(iter(LIMIT_FIELDS)))
        self.clear_key = tk.BooleanVar(self)
        self.show_advanced = tk.BooleanVar(self)
        self.thinking_mode = tk.StringVar(self, value="模型默认")
        self.active_label = tk.StringVar(self)
        self.key_label = tk.StringVar(self)
        self.status = tk.StringVar(self)
        self.test_status = tk.StringVar(self, value="连接状态：未验证；保存不代表连接成功。")
        self.config_path = tk.StringVar(self, value=str(self.store.path.resolve()))
        path_bar = ttk.LabelFrame(self, text="当前配置文件（不是密钥文件）", padding=5)
        path_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        # 正文布局在构建完成后统一下移一行。
        path_bar.columnconfigure(0, weight=1)
        path_entry = ttk.Entry(path_bar, textvariable=self.config_path, state="readonly", width=64)
        path_entry.grid(row=0, column=0, sticky="ew")
        self._tip(
            "config_path",
            path_entry,
            "普通配置明文保存参数和凭据引用；密钥在系统凭据库或进程内存。可复制此完整路径定位文件。",
        )
        path_actions = ttk.Frame(path_bar)
        path_actions.grid(row=1, column=0, sticky="w", pady=4)
        for col, (label, command) in enumerate(
            (
                ("选择文件", self.select_config),
                ("新建文件", self.create_config),
                ("使用说明", self.show_help),
            )
        ):
            button = ttk.Button(path_actions, text=label, command=command)
            button.grid(row=0, column=col, padx=(0, 6))
            if col == 2:
                self.help_button = button
            self._tip(
                f"path_action_{col}",
                button,
                (
                    "打开已有配置文件，不复制密钥或配置；未保存输入会先请求确认。",
                    "选择尚不存在的JSON路径；点击保存配置时才创建，不覆盖旧文件。",
                    "打开中文操作说明，包含本地／网络填写示例、密钥安全和连接测试边界。",
                )[col],
            )
        self.path_actions = path_actions
        ttk.Label(self, text="已有配置").grid(row=0, column=0, sticky="w")
        self.picker = ttk.Combobox(self, textvariable=self.choice, state="readonly", width=48)
        self.picker.grid(row=0, column=1, sticky="ew", pady=4)
        self.picker.bind("<<ComboboxSelected>>", self.load)
        self._tip(
            "picker",
            self.picker,
            "选择当前文件中的已有配置。加载不会读取或回显旧密钥，也不会设为活动或请求模型。",
        )
        for row, (name, label) in enumerate(
            (
                ("profile_id", "配置标识"),
                ("name", "显示名称"),
                ("base_url", "服务根地址"),
                ("model", "模型名称"),
                ("api_key", "新密钥（API Key）"),
            ),
            start=1,
        ):
            self._entry(self, row, name, label)
        self.entries["api_key"].configure(show="•")
        ttk.Label(
            self, text="本地与网络服务均填写地址；本地无认证时密钥留空。", wraplength=580
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(self, text="新密钥保存方式").grid(row=7, column=0, sticky="w")
        mode_picker = ttk.Combobox(
            self, textvariable=self.mode, values=list(MODES), state="readonly"
        )
        mode_picker.grid(row=7, column=1, sticky="ew", pady=4)
        self._tip(
            "mode",
            mode_picker,
            "系统凭据库：持久保存，其他同用户进程可读取。仅本次进程：只存内存，关闭GUI后不可恢复；不是跨进程密钥共享。",
        )
        ttk.Label(self, textvariable=self.key_label).grid(row=8, column=0, columnspan=2, sticky="w")
        ttk.Label(
            self,
            text="密钥框留空保留原引用，不回显旧密钥。仅本次进程不与其他进程共享。",
            wraplength=580,
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=4)
        clear_button = ttk.Checkbutton(
            self, text="清除当前配置的密钥引用（无认证服务）", variable=self.clear_key
        )
        clear_button.grid(row=10, column=0, columnspan=2, sticky="w")
        self._tip(
            "clear_key",
            clear_button,
            "勾选后清空新密钥输入，保存后取消此配置的认证。再次输入新密钥会取消勾选；旧历史凭据不立即删除。",
        )
        ttk.Checkbutton(
            self,
            text="显示高级配置（可选）",
            variable=self.show_advanced,
            command=self.toggle_advanced,
        ).grid(row=11, column=0, columnspan=2, sticky="w")
        self.advanced = ttk.LabelFrame(
            self, text="参数留空则不发送；支持范围以服务文档为准", padding=8
        )
        self.advanced.grid(row=12, column=0, columnspan=2, sticky="ew", pady=8)
        self.advanced.columnconfigure(1, weight=1)
        for row, (name, label) in enumerate(
            (
                ("temperature", "温度（0～2）"),
                ("max_output_tokens", "输出token上限"),
                ("connect_timeout_seconds", "连接超时（秒）"),
                ("read_timeout_seconds", "读取空闲超时（秒）"),
                ("thinking_json", "思考参数（JSON对象）"),
            )
        ):
            self._entry(self.advanced, row + (1 if name == "thinking_json" else 0), name, label)
        ttk.Label(self.advanced, text="思考请求（支持时） ⓘ").grid(row=4, column=0, sticky="w")
        self.thinking_picker = ttk.Combobox(
            self.advanced,
            textvariable=self.thinking_mode,
            values=list(THINKING_MODES),
            state="readonly",
        )
        self.thinking_picker.grid(row=4, column=1, sticky="ew", pady=4)
        self.thinking_picker.bind("<<ComboboxSelected>>", self.apply_thinking_mode)
        self._tip(
            "thinking_mode",
            self.thinking_picker,
            "默认不发送思考参数；开启／关闭写入reasoning_effort=medium／none。"
            "服务与模型须支持，不保证生效或强度相同；可用自定义JSON适配其他服务。"
            "恢复默认会清空思考JSON，不会调用模型。",
        )
        ttk.Label(self.advanced, text="输出上限字段").grid(row=6, column=0, sticky="w")
        limit_picker = ttk.Combobox(
            self.advanced,
            textvariable=self.limit_field,
            values=list(LIMIT_FIELDS),
            state="readonly",
            width=36,
        )
        limit_picker.grid(row=6, column=1, sticky="ew", pady=4)
        self._tip(
            "limit_field",
            limit_picker,
            "只在填写输出上限时生效。按服务文档选择字段，不同时发送两个上限，不自动推测模型支持情况。",
        )
        self.buttons = {}
        self._build_probe_panel()
        actions = ttk.Frame(self)
        actions.grid(row=13, column=0, columnspan=2, sticky="w", pady=8)
        for column, (name, label, command) in enumerate(
            (
                ("new", "新增", self.new),
                ("save", "保存配置", self.save),
                ("activate", "设为活动模型", self.activate),
                ("delete", "删除配置", self.delete),
                ("migrate", "迁移旧配置", self.migrate),
                ("test", "连接测试", self.test_connection),
                ("cancel", "取消测试", self.cancel_test),
            )
        ):
            parent = self.probe_controls if name in {"test", "cancel"} else actions
            button = ttk.Button(parent, text=label, command=command)
            if parent is self.probe_controls:
                button.grid(row=0, column=column - 5, padx=(0, 6), pady=3)
            else:
                button.grid(row=column // 4, column=column % 4, padx=(0, 6), pady=3)
            self.buttons[name] = button
        self.buttons["help"] = self.help_button
        self._tip(
            "test",
            self.buttons["test"],
            "真实调用当前已保存配置，可能计费；单条独立消息，无NPC上下文。"
            "总时限30秒，输出至多64 token，不修改活动配置。",
        )
        self._tip(
            "activate",
            self.buttons["activate"],
            "仅修改活动配置选择，不发起连接、不影响已经取得的调用快照；不保证其他项目自动采用此配置。",
        )
        self.buttons["cancel"].configure(state="disabled")
        for name, description in {
            "new": "清空表单以新建配置，不立即写文件；已有未保存修改会先请求确认。",
            "save": "验证并保存参数及凭据引用，不请求模型；成功不代表地址或模型可用。",
            "delete": "确认后删除当前配置，不立即删除历史凭据；删除活动配置会清空活动选择。",
            "migrate": "仅旧格式可用。确认后移除原文件明文密钥，不建明文备份；失败保留原文件。",
            "cancel": "请求停止当前独立测试的本地接收并释放连接，不能保证远端停止生成或计费。",
        }.items():
            self._tip(name, self.buttons[name], description)
        ttk.Label(self, textvariable=self.active_label).grid(
            row=14, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(self, textvariable=self.status, wraplength=580).grid(
            row=15, column=0, columnspan=2, sticky="w", pady=6
        )
        # 原表单整体下移一行，顶部留给配置路径；不改变高级配置内部布局。
        for child in self.winfo_children():
            if child not in (path_bar, self.probe_panel):
                child.grid_configure(row=int(child.grid_info()["row"]) + 1)
        self.clear_key.trace_add("write", self._key_action_changed)
        self.values["api_key"].trace_add("write", self._key_action_changed)
        for variable in (*self.values.values(), self.clear_key, self.limit_field):
            variable.trace_add("write", self._invalidate_test)
        self.values["thinking_json"].trace_add("write", self._sync_thinking_mode)
        self.new(_confirm=False)
        self.toggle_advanced()
        self.refresh()

    def _build_probe_panel(self):
        self.probe_panel = ttk.LabelFrame(self, text="独立连接测试／实时诊断", padding=10)
        self.probe_panel.grid(row=0, column=2, rowspan=18, sticky="nsew", padx=(18, 0))
        self.probe_panel.columnconfigure(0, weight=1)
        self.probe_panel.rowconfigure(6, weight=1)
        ttk.Label(
            self.probe_panel,
            text="仅确认后真实调用；不带NPC背景或历史。\n总时限30秒，输出至多64 token。",
            wraplength=390,
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.probe_controls = ttk.Frame(self.probe_panel)
        self.probe_controls.grid(row=1, column=0, sticky="w")
        ttk.Label(self.probe_panel, textvariable=self.test_status, wraplength=390).grid(
            row=2, column=0, sticky="ew", pady=8
        )
        self.probe_stage = tk.StringVar(self, value="当前阶段：等待启动")
        self.probe_target = tk.StringVar(
            self, value="被测配置：尚未选择；下方仅显示本次或上次测试证据。"
        )
        self.probe_metrics = tk.StringVar(self, value="HTTP状态与数据统计：尚未取得")
        self.probe_hint = tk.StringVar(
            self, value="阶段依据客户端观测；模型加载、排队需查服务端日志。"
        )
        ttk.Label(self.probe_panel, textvariable=self.probe_stage, wraplength=390).grid(
            row=3, column=0, sticky="ew"
        )
        self.probe_bar = ttk.Progressbar(self.probe_panel, mode="indeterminate")
        self.probe_bar.grid(row=4, column=0, sticky="ew", pady=6)
        ttk.Label(self.probe_panel, textvariable=self.probe_metrics, wraplength=390).grid(
            row=5, column=0, sticky="ew", pady=5
        )
        log_frame = ttk.LabelFrame(self.probe_panel, text="里程碑时间线（不含请求或生成原文）")
        log_frame.grid(row=6, column=0, sticky="nsew", pady=5)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.probe_log = tk.Text(log_frame, width=45, height=13, wrap="word", state="disabled")
        self.probe_log.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(log_frame, command=self.probe_log.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.probe_log.configure(yscrollcommand=bar.set)
        ttk.Label(self.probe_panel, textvariable=self.probe_hint, wraplength=390).grid(
            row=7, column=0, sticky="ew", pady=5
        )
        ttk.Label(self.probe_panel, textvariable=self.probe_target, wraplength=390).grid(
            row=8, column=0, sticky="ew", pady=5
        )
        self.show_content = tk.BooleanVar(self, value=False)
        self.content_toggle = ttk.Checkbutton(
            self.probe_panel,
            text="采集并查看返回内容（测试前选择，仅内存）",
            variable=self.show_content,
            command=self.toggle_content,
        )
        self.content_toggle.grid(row=9, column=0, sticky="w")
        self._tip(
            "show_content",
            self.content_toggle,
            "默认不采集。勾选后再测试，分别显示正文和服务返回的独立思考字段；"
            "各最多4096字符，不落盘、不进入时间线或NPC记忆。取消勾选清空。"
            "仅隐藏已知API密钥原值，不保证屏蔽全部敏感内容。",
        )
        self.content_frame = ttk.LabelFrame(
            self.probe_panel, text="可选返回内容（不是完整内部推理）"
        )
        self.content_frame.grid(row=10, column=0, sticky="ew", pady=5)
        self.content_frame.columnconfigure(0, weight=1)
        book = ttk.Notebook(self.content_frame)
        book.grid(row=0, column=0, sticky="ew")
        self.content_texts = {}
        for name, title in (("text", "回复正文"), ("reasoning", "服务思考字段")):
            tab = ttk.Frame(book)
            tab.columnconfigure(0, weight=1)
            text = tk.Text(tab, width=43, height=6, wrap="word", state="disabled")
            text.grid(row=0, column=0, sticky="ew")
            bar = ttk.Scrollbar(tab, command=text.yview)
            bar.grid(row=0, column=1, sticky="ns")
            text.configure(yscrollcommand=bar.set)
            self.content_texts[name] = text
            book.add(tab, text=title)
        self.content_note = tk.StringVar(self, value="勾选后重新测试才会采集；不读取历史回复。")
        ttk.Label(self.content_frame, textvariable=self.content_note, wraplength=380).grid(
            row=1, column=0, sticky="ew"
        )
        self.content_frame.grid_remove()

    def _clear_content(self):
        for text in self.content_texts.values():
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.configure(state="disabled")
        self.content_note.set("勾选后重新测试才会采集；不读取历史回复。")

    def toggle_content(self):
        if self.show_content.get():
            self.content_frame.grid()
            self.probe_log.configure(height=7)
        else:
            self.content_frame.grid_remove()
            self.probe_log.configure(height=13)
            self._clear_content()

    def _render_content(self, preview):
        if not self.show_content.get() or preview is None:
            return
        for name, text in self.content_texts.items():
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", preview[name])
            text.configure(state="disabled")
        self.content_note.set(
            "预览已截断，各最多4096字符；完整统计仍在上方。"
            if preview["truncated"]
            else "仅本次内存预览；思考字段为空不代表模型内部没有推理。"
        )

    def _sync_thinking_mode(self, *_):
        try:
            text = self.values["thinking_json"].get().strip()
            settings = _json(text) if text else {}
            if not isinstance(settings, dict):
                raise ValueError
            if not settings:
                choice = "模型默认"
            elif (
                settings.get("reasoning_effort") in ("medium", "none")
                and "reasoning" not in settings
            ):
                choice = (
                    "请求开启（medium）"
                    if settings["reasoning_effort"] == "medium"
                    else "请求关闭（none）"
                )
            else:
                choice = "自定义JSON"
        except ValueError:
            choice = "自定义JSON"
        self.thinking_mode.set(choice)

    def apply_thinking_mode(self, _event=None):
        choice = self.thinking_mode.get()
        if choice == "自定义JSON":
            self.entries["thinking_json"].focus_set()
            return
        try:
            text = self.values["thinking_json"].get().strip()
            settings = _json(text) if text else {}
            if not isinstance(settings, dict):
                raise ValueError("思考参数须为JSON对象，请修正或清空后选择预设。")
            if choice == "模型默认":
                if settings and settings not in (
                    {"reasoning_effort": "medium"},
                    {"reasoning_effort": "none"},
                ):
                    if not messagebox.askyesno(
                        "恢复模型默认",
                        "将清空自定义思考JSON，是否继续？",
                        parent=self.winfo_toplevel(),
                    ):
                        self._sync_thinking_mode()
                        return
                settings = {}
            else:
                if "reasoning" in settings:
                    if not isinstance(settings["reasoning"], dict):
                        raise ValueError("自定义reasoning字段不兼容预设，请先修正或清空。")
                    settings["reasoning"].pop("effort", None)
                    if not settings["reasoning"]:
                        del settings["reasoning"]
                settings["reasoning_effort"] = THINKING_MODES[choice]
            self.values["thinking_json"].set(json.dumps(settings, ensure_ascii=False))
        except (ValueError, KeyError):
            self._sync_thinking_mode()
            self._error("预设未应用", ValueError("思考JSON无效或与预设不兼容；原输入未修改。"))

    def _append_probe_log(self, text):
        self.probe_log.configure(state="normal")
        self.probe_log.insert("end", text + "\n")
        self.probe_log.see("end")
        self.probe_log.configure(state="disabled")

    def _render_progress(self, info):
        self.probe_last_progress = info
        stage = info.get("stage", "prepared")
        elapsed = (
            perf_counter() - self.probe_started
            if self.probe_job is not None and self.probe_started is not None
            else info.get("elapsed_s", 0)
        )
        stage_name = STAGE_NAMES.get(stage, "未知阶段")
        if stage == "closed":
            stage_name += "；终止前：" + STAGE_NAMES.get(info.get("last_stage"), "未知")
        self.probe_stage.set(f"当前阶段：{stage_name}；已用{elapsed:.2f}秒")
        timeline = info.get("timeline", [])
        for item in timeline[self.probe_logged :]:
            name = STAGE_NAMES.get(item["stage"], "未知阶段")
            self._append_probe_log(f"{item['elapsed_s']:.3f}秒  {name}")
        self.probe_logged = len(timeline)
        raw_reason = info.get("raw_reason")
        reason_label = {
            "stop": "stop（正常停止）",
            "length": "length（长度截断）",
            "content_filter": "content_filter（服务过滤）",
        }.get(raw_reason, "未收到")
        error = info.get("error_type")
        self.probe_metrics.set(
            f"HTTP状态：{info.get('http_status', '未收到')}；"
            f"类型：{info.get('content_type', '未知')}\n"
            f"响应体：{info.get('received_bytes', 0)}字节／{info.get('network_chunks', 0)}数据块；"
            f"有效事件：{info.get('event_count', 0)}\n"
            f"思考：{info.get('reasoning_chunks', 0)}片／{info.get('reasoning_chars', 0)}字；"
            f"正文：{info.get('text_chunks', 0)}片／{info.get('text_chars', 0)}字\n"
            f"服务结束原因：{reason_label}；"
            f"异常类型：{ERROR_NAMES.get(error, '其他传输异常' if error else '未记录')}"
        )
        if self.probe_job is not None:
            self.probe_hint.set("进行中；以上为累计观测，不是模型内部状态。")
        else:
            self.probe_hint.set(diagnostic_hint(info))

    def _entry(self, parent, row, name, label):
        caption = ttk.Label(parent, text=label + " ⓘ")
        caption.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        entry = ttk.Entry(parent, textvariable=self.values[name], width=48)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.entries[name] = entry
        self._tip(name, entry, FIELD_HELP[name])
        Tooltip(caption, FIELD_HELP[name])

    def _tip(self, name, widget, text):
        self.tooltips[name] = Tooltip(widget, text)

    def _key_action_changed(self, name, *_):
        if name == str(self.clear_key) and self.clear_key.get():
            self.values["api_key"].set("")
        elif name == str(self.values["api_key"]) and self.values["api_key"].get():
            self.clear_key.set(False)

    def _invalidate_test(self, *_):
        self.test_status.set("连接状态：未验证／表单已变化；保存不代表连接成功。")

    def _has_changes(self):
        if self.loaded is None:
            return any(
                v.get()
                for k, v in self.values.items()
                if k not in {"connect_timeout_seconds", "read_timeout_seconds"}
            ) or (
                self.values["connect_timeout_seconds"].get() != "10"
                or self.values["read_timeout_seconds"].get() != "60"
                or self.clear_key.get()
                or self.limit_field.get() != next(iter(LIMIT_FIELDS))
            )
        try:
            return bool(
                self._profile() != self.loaded
                or self.values["api_key"].get()
                or self.clear_key.get()
            )
        except ValueError:
            return True

    def _allow_discard(self):
        return not self._has_changes() or messagebox.askyesno(
            "未保存的修改", "当前输入尚未保存。是否放弃这些修改？", parent=self.winfo_toplevel()
        )

    def select_config(self):
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="选择模型配置文件",
            filetypes=[("JSON配置", "*.json"), ("全部文件", "*")],
        )
        if path:
            self.use_config(Path(path))

    def create_config(self):
        path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(),
            title="选择新配置文件位置",
            defaultextension=".json",
            initialfile="profiles.json",
        )
        if path:
            if Path(path).exists():
                messagebox.showerror(
                    "新建失败",
                    "路径已存在，请用选择文件打开，不覆盖旧配置。",
                    parent=self.winfo_toplevel(),
                )
                return
            self.use_config(Path(path))

    def use_config(self, path):
        if self.probe_job is not None or not self._allow_discard():
            return
        candidate = ProfileStore(path, credentials=self.store.credentials)
        try:
            if not candidate.is_legacy():
                candidate.list_profiles()
        except (ValueError, OSError) as error:
            self._error("读取失败", error)
            return
        self.store = candidate
        self.config_path.set(str(self.store.path.resolve()))
        self.new(_confirm=False)
        self.refresh()

    def show_help(self):
        if self.help_window is not None and self.help_window.winfo_exists():
            self.help_window.lift()
            return
        window = self.help_window = tk.Toplevel(self)
        window.title("模型配置使用说明")
        text = tk.Text(window, wrap="word", width=76, height=28, padx=12, pady=12)
        bar = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", GUI_HELP)
        text.configure(state="disabled")

    def _saved_profile(self):
        try:
            profile = self.store.get(self.values["profile_id"].get().strip())
        except KeyError:
            raise ValueError("请先保存当前配置。") from None
        if profile != self._profile() or self.values["api_key"].get() or self.clear_key.get():
            raise ValueError("请先保存当前修改，再进行连接测试。")
        return profile

    def test_connection(self):
        if self.probe_job is not None:
            return
        try:
            profile = self._saved_profile()
        except (ValueError, OSError) as error:
            self._error("测试未开始", error)
            return
        if not messagebox.askyesno(
            "确认独立连接测试",
            f"将真实调用配置“{profile.name}”，网络服务可能产生费用。\n"
            f"单条短消息，不带NPC上下文；输出上限至多{PROBE_MAX_TOKENS} token，"
            f"总时限{PROBE_TIMEOUT_SECONDS:g}秒。\n不保存回复正文、不修改活动配置。是否继续？",
            parent=self.winfo_toplevel(),
        ):
            return
        self.probe_profile_id = profile.profile_id
        self.probe_thinking_request = profile.generation_parameters().get("reasoning_effort")
        self.probe_target.set(
            f"被测配置快照：{profile.profile_id}；思考选项：{self.thinking_mode.get()}。"
            "修改表单不会改变本次请求，是否支持由服务决定。"
        )
        self.probe_started = perf_counter()
        self.probe_logged = 0
        self.probe_last_progress = {}
        self.probe_log.configure(state="normal")
        self.probe_log.delete("1.0", "end")
        self.probe_log.configure(state="disabled")
        self.probe_hint.set("进行中；可随时取消本地接收。")
        self.probe_bar.start(50)
        self._clear_content()
        self.content_toggle.configure(state="disabled")
        self.probe_job = ProbeJob(
            ModelService(self.store), profile, capture_content=self.show_content.get()
        )
        self._render_progress({"stage": "prepared", "elapsed_s": 0, "timeline": []})
        self.test_status.set(f"连接状态：正在测试配置 {profile.profile_id}，可取消。")
        for name in ("test", "new", "save", "activate", "delete", "migrate"):
            self.buttons[name].configure(state="disabled")
        self.buttons["cancel"].configure(state="normal")
        self.picker.configure(state="disabled")
        for button in self.path_actions.winfo_children()[:2]:
            button.configure(state="disabled")
        self.probe_job.start()
        self.probe_poll = self.after(50, self._poll_probe)

    def _poll_probe(self):
        self.probe_poll = None
        try:
            self._render_content(self.probe_job.contents.get_nowait())
        except Empty:
            pass
        try:
            self._render_progress(self.probe_job.progress.get_nowait())
        except Empty:
            self._render_progress(self.probe_last_progress)
        try:
            result = self.probe_job.results.get_nowait()
        except Empty:
            self.probe_poll = self.after(50, self._poll_probe)
            return
        self.probe_job = None
        self.content_toggle.configure(state="normal")
        self._render_content(result.preview)
        self.probe_bar.stop()
        self._render_progress(result.diagnostics)
        self.picker.configure(state="readonly")
        for button in self.path_actions.winfo_children()[:2]:
            button.configure(state="normal")
        self.buttons["cancel"].configure(state="disabled")
        self.buttons["new"].configure(state="normal")
        self.refresh()
        first = "未收到" if result.first_text_s is None else f"{result.first_text_s:.3f}秒"
        label = {"passed": "通过", "failed": "未通过", "cancelled": "已取消", "timeout": "总超时"}
        self.test_status.set(
            f"配置 {self.probe_profile_id}：{label[result.outcome]}；首片{first}，"
            f"总耗时{result.elapsed_s:.3f}秒，正文{result.text_chars}字。{result.message}"
            + (" 当前表单有修改，结果不适用于未保存内容。" if self._has_changes() else "")
        )
        self._append_probe_log(
            f"{result.elapsed_s:.3f}秒  {label[result.outcome]}：{result.message}"
        )
        if result.outcome == "passed":
            self.probe_hint.set("本次纯文本调用通过，不代表NPC认知质量或正式性能验收通过。")
            if self.probe_thinking_request == "none" and result.diagnostics.get(
                "reasoning_chars", 0
            ):
                self.probe_hint.set(
                    "调用通过，但请求关闭后仍收到思考字段；服务可能未支持或未执行关闭请求。"
                )
            elif self.probe_thinking_request == "medium" and not result.diagnostics.get(
                "reasoning_chars", 0
            ):
                self.probe_hint.set("调用通过，但未观察到独立思考字段；不能确认开启请求已生效。")
        elif result.outcome == "cancelled":
            self.probe_hint.set("用户已取消，本次不判定模型可用性；远端可能仍在生成。")
        if self.close_pending:
            self.winfo_toplevel().destroy()

    def cancel_test(self):
        if self.probe_job is not None:
            self.probe_job.cancel()
            self.test_status.set("正在取消本地接收；不保证远端停止生成或计费。")

    def close(self, *, confirm=True):
        if confirm and not self._allow_discard():
            return
        if self.probe_job is not None:
            self.close_pending = True
            self.cancel_test()
        else:
            self.winfo_toplevel().destroy()

    def toggle_advanced(self):
        if self.show_advanced.get():
            self.advanced.grid()
        else:
            self.advanced.grid_remove()

    def _error(self, title, error):
        self.status.set(f"{title}，未完成操作。")
        messagebox.showerror(title, str(error), parent=self.winfo_toplevel())

    def refresh(self):
        try:
            legacy = self.store.is_legacy()
            for name in ("save", "activate", "delete", "test"):
                self.buttons[name].configure(state="disabled" if legacy else "normal")
            self.buttons["migrate"].configure(state="normal" if legacy else "disabled")
            if legacy:
                self.picker["values"] = []
                self.active_label.set("旧配置尚未迁移。")
                self.status.set("检测到旧配置，尚未自动修改。请选保存方式，再点击迁移旧配置。")
                return
            self.picker["values"] = [p.profile_id for p in self.store.list_profiles()]
            try:
                self.active_label.set("当前活动配置：" + self.store.active_profile().name)
            except ValueError:
                self.active_label.set("当前活动配置：未选择")
        except (ValueError, OSError) as error:
            self._error("读取失败", error)

    def new(self, *, _confirm=True):
        if _confirm and not self._allow_discard():
            return
        self.loaded = None
        self.choice.set("")
        for variable in self.values.values():
            variable.set("")
        self.entries["profile_id"].configure(state="normal")
        self.values["connect_timeout_seconds"].set("10")
        self.values["read_timeout_seconds"].set("60")
        self.mode.set("系统凭据库")
        self.limit_field.set(next(iter(LIMIT_FIELDS)))
        self.clear_key.set(False)
        self.key_label.set("当前凭据：未配置")
        self.status.set("填写后保存，再设为活动模型；保存不会调用模型。")

    def load(self, _event=None, *, _confirm=True):
        if _confirm and not self._allow_discard():
            self.choice.set(self.loaded.profile_id if self.loaded else "")
            return
        try:
            profile = self.store.get(self.choice.get())
            self.loaded = profile
            for name, variable in self.values.items():
                if name == "api_key":
                    variable.set("")
                else:
                    value = getattr(profile, name)
                    variable.set("" if value is None else str(value))
            self.entries["profile_id"].configure(state="readonly")
            self.clear_key.set(False)
            self.limit_field.set(
                next(k for k, v in LIMIT_FIELDS.items() if v == profile.token_limit_field)
            )
            if profile.credential_ref:
                session = profile.credential_ref.startswith("session:")
                self.mode.set("仅本次进程" if session else "系统凭据库")
                self.key_label.set(
                    "当前凭据："
                    + ("仅进程引用，重开可能需重新输入" if session else "系统库引用，未读取密钥")
                )
            else:
                self.key_label.set("当前凭据：未配置")
            self.status.set("已加载配置；没有读取或回显旧密钥。")
        except (KeyError, ValueError, OSError) as error:
            self._error("加载失败", error)

    def _profile(self):
        def optional(name, conversion, default=None):
            value = self.values[name].get().strip()
            if not value:
                return default
            try:
                return conversion(value)
            except ValueError:
                raise ValueError("可选参数数值格式无效。") from None

        reference = self.loaded.credential_ref if self.loaded else None
        if self.clear_key.get():
            reference = None
        return ModelProfile(
            profile_id=self.values["profile_id"].get().strip(),
            name=self.values["name"].get().strip(),
            base_url=self.values["base_url"].get().strip(),
            model=self.values["model"].get().strip(),
            credential_ref=reference,
            json_mode=self.loaded.json_mode if self.loaded else False,
            timeout_seconds=self.loaded.timeout_seconds if self.loaded else 60,
            connect_timeout_seconds=optional("connect_timeout_seconds", float, 10),
            read_timeout_seconds=optional("read_timeout_seconds", float, 60),
            temperature=optional("temperature", float),
            max_output_tokens=optional("max_output_tokens", int),
            token_limit_field=LIMIT_FIELDS[self.limit_field.get()],
            thinking_json=self.values["thinking_json"].get(),
        )

    def save(self):
        try:
            profile = self.store.save(
                self._profile(),
                api_key=self.values["api_key"].get() or None,
                credential_mode=MODES[self.mode.get()],
                clear_key=self.clear_key.get(),
            )
            self.values["api_key"].set("")
            self.choice.set(profile.profile_id)
            self.load(_confirm=False)
            self.refresh()
            self.status.set("已保存配置；密钥不在普通配置文件中。未发起模型请求。")
        except (ValueError, OSError, CredentialError) as error:
            self._error("保存失败", error)

    def activate(self):
        try:
            profile = self.store.get(self.values["profile_id"].get().strip())
            if profile != self._profile() or self.values["api_key"].get() or self.clear_key.get():
                raise ValueError("请先保存当前修改，再设为活动模型。")
            self.store.activate(profile.profile_id)
            self.refresh()
            self.status.set("活动配置已切换；已取得的调用快照不变。")
        except (KeyError, ValueError, OSError) as error:
            self._error("切换失败", error)

    def delete(self):
        profile_id = self.values["profile_id"].get().strip()
        if not messagebox.askyesno(
            "删除配置",
            "只删除所选配置，不立即清理历史凭据，以保留已冻结调用。是否继续？",
            parent=self.winfo_toplevel(),
        ):
            return
        try:
            self.store.delete(profile_id)
            self.new(_confirm=False)
            self.refresh()
            self.status.set("配置已删除；已有快照未改变。")
        except (KeyError, ValueError, OSError) as error:
            self._error("删除失败", error)

    def migrate(self):
        prompt = (
            f"将旧密钥迁移到“{self.mode.get()}”，成功后更新原配置并移除明文。"
            "不另建明文备份，失败保留原文件。仅进程方式不供其他进程使用。是否继续？"
        )
        if not messagebox.askyesno("确认迁移旧配置", prompt, parent=self.winfo_toplevel()):
            return
        try:
            count = self.store.migrate_legacy(credential_mode=MODES[self.mode.get()])
            self.new(_confirm=False)
            self.refresh()
            self.status.set(f"已迁移{count}组配置；普通配置不再含明文密钥。")
        except (ValueError, OSError, CredentialError) as error:
            self._error("迁移失败", error)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="模型服务中文配置界面；仅确认独立连接测试时调用模型。",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出。")
    parser.add_argument("--config", type=Path, help="指定配置文件；省略时使用当前用户配置。")
    args = parser.parse_args()
    root = tk.Tk()
    ModelConfigApp(root, ProfileStore(args.config))
    root.mainloop()


if __name__ == "__main__":
    main()
