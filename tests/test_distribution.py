"""普通分发wheel的结构验证；在临时副本构建，不污染源码或使用用户配置。"""

import configparser
import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    root = tmp_path_factory.mktemp("model-service-wheel")
    source = root / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(PACKAGE / name, source / name)
    shutil.copytree(PACKAGE / "src/model_service", source / "src/model_service",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # 将敏感文件放进构建输入，确保默认数据收集不会把它们装进wheel。
    (source / "src/model_service/profiles.json").write_text('{"canary":"not-a-key"}')
    (source / ".env").write_text("CANARY=not-a-key")
    (source / "src/unrelated_project").mkdir()
    (source / "src/unrelated_project/__init__.py").write_text("")
    output = root / "dist"
    output.mkdir()
    subprocess.run(
        [sys.executable, "-I", "-c",
         "from setuptools.build_meta import build_wheel; build_wheel(" + repr(str(output)) + ")"],
        cwd=source, check=True, capture_output=True, text=True,
    )
    artifacts = list(output.glob("*.whl"))
    assert len(artifacts) == 1
    return artifacts[0]


def test_regular_wheel_metadata_and_entrypoint(wheel):
    assert wheel.name == "model_service_kit-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(
            archive.read("model_service_kit-0.1.0.dist-info/METADATA")
        )
        assert metadata["Name"] == "model-service-kit" and metadata["Version"] == "0.1.0"
        assert metadata["Requires-Python"] == ">=3.12"
        assert {r for r in metadata.get_all("Requires-Dist") if ";" not in r} == {
            "httpx<0.29,>=0.28", "keyring<26,>=25",
        }
        assert metadata["License-Expression"] == "MIT"
        assert metadata["Author"] == "model-service-kit contributors"
        assert metadata["Author-email"] is None
        assert "model_service_kit-0.1.0.dist-info/licenses/LICENSE" in archive.namelist()
        entrypoints = configparser.ConfigParser()
        entrypoints.read_string(archive.read(
            "model_service_kit-0.1.0.dist-info/entry_points.txt"
        ).decode())
        assert entrypoints["console_scripts"]["model-service-config"] == "model_service.gui:main"
        wheel_metadata = archive.read("model_service_kit-0.1.0.dist-info/WHEEL").decode()
        assert "Root-Is-Purelib: true" in wheel_metadata
        assert "Tag: py3-none-any" in wheel_metadata


def test_wheel_contains_only_library_code_and_metadata(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        expected = {"__init__.py", "client.py", "config.py", "credentials.py", "gui.py",
                    "probe.py", "streaming.py"}
        assert {n.removeprefix("model_service/") for n in names
                if n.startswith("model_service/")} == expected
        assert all(n.startswith(("model_service/", "model_service_kit-0.1.0.dist-info/"))
                   for n in names)
        assert not any("editable" in n.lower() or n.endswith((".pth", ".pyc")) for n in names)
        assert b'"canary"' not in b"".join(archive.read(n) for n in names)


def test_package_version_is_independent_of_npc():
    package = tomllib.loads((PACKAGE / "pyproject.toml").read_text("utf-8"))["project"]
    assert package["version"] == "0.1.0"
    assert "npc_agent" not in json.dumps(package)
    assert package["license"] == "MIT"
