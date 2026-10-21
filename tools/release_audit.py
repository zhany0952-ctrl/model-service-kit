"""只扫描明确的候选文件；报告不含匹配原文，不读取配置或系统凭据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "open-model-config.cmd",
}
DIRECTORIES = {"src", "tests", "tools", "docs", "examples", "distribution"}
SUFFIXES = {".py", ".md", ".toml", ".cmd"}
PATTERNS = {
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "provider_token": r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,})\b",
    "absolute_windows_path": r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]",
    "personal_directory": r"(?:Users[\\/]|/home/)[^\\/\s\"']+",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
}
PUBLIC_DOC_HOSTS = {
    "docs.ollama.com",
    "api-docs.deepseek.com",
    "www.encode.io",
    "developers.openai.com",
    "packaging.python.org",
    "pip.pypa.io",
    "keyring.readthedocs.io",
    "docs.github.com",
    "github.com",
}
URL_PATTERN = re.compile(r"https?://[^\s\"<>\)\]']+")


def candidate_files(root=ROOT):
    """白名单导出集合；不是递归上传整个工作目录。"""
    root = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        selected = (
            relative.as_posix() in TOP_LEVEL
            or relative.parts[0] in DIRECTORIES
            and path.suffix in SUFFIXES
        )
        if not selected:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("候选文件不能是符号链接或指向项目外。")
        yield path


def audit(root=ROOT):
    root = root.resolve()
    files, findings = [], []
    for path in candidate_files(root):
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        text = data.decode("utf-8")
        files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})

        def finding(code, offset, *, level="review"):
            findings.append(
                {
                    "path": name,
                    "line": text.count("\n", 0, offset) + 1,
                    "code": code,
                    "level": level,
                }
            )

        for code, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                # 明确的测试域名邮箱只作误报登记，不回显匹配值。
                fake = code == "email" and match.group().endswith(".test")
                finding(code, match.start(), level="example" if fake else "review")
        for match in URL_PATTERN.finditer(text):
            try:
                parsed = urlsplit(match.group())
                host = parsed.hostname or ""
            except ValueError:
                finding("malformed_url_fixture", match.start())
                continue
            safe = (
                host in PUBLIC_DOC_HOSTS
                or host in {"127.0.0.1", "localhost", "你的服务域名"}
                or host.endswith((".test", ".invalid", ".example"))
            )
            if not safe:
                finding("unreviewed_endpoint", match.start())
            if parsed.username or parsed.password:
                finding(
                    "url_userinfo",
                    match.start(),
                    level="example" if host.endswith(".test") else "review",
                )
    return {
        "candidate_files": files,
        "findings": findings,
        "requires_human_confirmation": True,
        "note": "模式扫描不是安全保证；上传前还须检查Git元数据和每个分发附件。",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="必须是仓库外且不存在的路径。")
    args = parser.parse_args()
    report_path = args.report.resolve()
    if report_path.is_relative_to(ROOT) or report_path.exists():
        parser.error("报告必须位于仓库外，且不能覆盖已有文件。")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    result = audit()
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(f"候选文件：{len(result['candidate_files'])}；待解释项：{len(result['findings'])}。")
    print("没有创建提交、远程仓库或发送数据；上传前须人工确认。")
