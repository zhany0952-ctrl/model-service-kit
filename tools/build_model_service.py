"""构建模型服务wheel及可携带手册，不安装包、不读取配置、不公开发布。"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT


def build(output, *, no_isolation=False):
    version = tomllib.loads((PACKAGE / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # 不覆盖旧交付件。
    command = [sys.executable, "-I", "-m", "build", "--wheel", "--outdir", str(output)]
    if no_isolation:
        command.append("--no-isolation")
    subprocess.run(command + [str(PACKAGE)], check=True, cwd=ROOT)
    wheel = output / f"model_service_kit-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        assert all(name.startswith(("model_service/", f"model_service_kit-{version}.dist-info/"))
                   for name in archive.namelist())
        assert all(not name.endswith((".json", ".sqlite3", ".pyc", ".pth"))
                   for name in archive.namelist())
    files = {
        "README.md": "README.md",
        "LICENSE": "LICENSE",
        "docs/publishing-security.md": "docs/publishing-security.md",
        "docs/installation.md": "docs/installation.md",
        "docs/integration.md": "docs/integration.md",
        "docs/interface.md": "docs/interface.md",
        "examples/stream_from_config.py": "examples/stream_from_config.py",
        "distribution/open-model-config.cmd": "open-model-config.cmd",
    }
    for source, target in files.items():
        destination = output / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGE / source, destination)
    checksums = []
    for path in sorted(p for p in output.rglob("*") if p.is_file()):
        checksums.append(hashlib.sha256(path.read_bytes()).hexdigest()
                         + "  " + path.relative_to(output).as_posix())
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print("交付目录：" + str(output))
    return wheel


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--no-isolation", action="store_true",
                        help="仅在已准备好build和setuptools的独立构建环境使用。")
    args = parser.parse_args()
    build(args.output_dir, no_isolation=args.no_isolation)
